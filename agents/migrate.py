"""Move the hub's data (chats, accounts, keys, workspaces) from one GPU box to another, e.g. to a bigger disk
or onto a persistent volume, without losing anything.

New box:  started with RESTORE_KEY (and RESTORE_PORT exposed); supervise.mjs runs `migrate.py receive`
          before anything else touches the data folder, and holds the tunnel until the data has arrived.
Old box:  the console's POST /api/admin/migrate {target: "http://<new ip>:<port>", key} streams everything:
          databases are copied with SQLite's backup API (consistent while running), then tar+gzip, sent in
          AES-GCM-sealed frames (the network path is plain HTTP; the key never travels).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import queue
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FRAME = 1024 * 1024
END = b"MODEL-HUB-END"
SKIP_DIRS = {"hub-code", "cache", ".cache", "__pycache__"}
SKIP_SUFFIXES = ("-wal", "-shm", "-journal", ".tmp")
DATABASES = (".db", ".sqlite", ".sqlite3")
SEND: dict = {"state": "idle"}


def _key(secret: str) -> bytes:
    return hashlib.sha256(("model-hub-migrate:" + secret).encode()).digest()


def _nonce(n: int) -> bytes:
    return n.to_bytes(12, "big")


class _QueueWriter(io.RawIOBase):
    def __init__(self, q: queue.Queue):
        self.q = q

    def writable(self):
        return True

    def write(self, data):
        self.q.put(bytes(data))
        return len(data)


def _add_tree(tar: tarfile.TarFile, root: Path, prefix: str, scratch: Path, counter: dict):
    if not root.is_dir():
        return
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and ".before-restore-" not in d]
        rel = Path(folder).relative_to(root)
        tar.add(folder, arcname=str(Path(prefix) / rel), recursive=False)
        for name in files:
            path = Path(folder) / name
            if name.endswith(SKIP_SUFFIXES) or (path.parent == root and name in {"status.json", "controller.lock"}):
                continue
            arcname = str(Path(prefix) / rel / name)
            try:
                if name.endswith(DATABASES) and not path.is_symlink():
                    copy = scratch / f"{counter['files']}.db"
                    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
                    target = sqlite3.connect(copy)
                    with target:
                        source.backup(target)
                    source.close(); target.close()
                    info = tar.gettarinfo(str(path), arcname=arcname)
                    info.size = copy.stat().st_size
                    with copy.open("rb") as handle:
                        tar.addfile(info, handle)
                    copy.unlink()
                else:
                    tar.add(str(path), arcname=arcname, recursive=False)
                counter["files"] += 1
            except (OSError, sqlite3.Error) as error:
                counter.setdefault("skipped", []).append(f"{arcname}: {type(error).__name__}")


def frames(secret: str, sources: list[tuple[Path, str]], status: dict):
    """Encrypted archive frames: [4-byte length][ciphertext], ending with a sealed END frame and a zero length."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes, n = AESGCM(_key(secret)), 0
    q: queue.Queue = queue.Queue(maxsize=64)
    counter = {"files": 0}

    def produce():
        try:
            with tempfile.TemporaryDirectory(prefix="hub-migrate-") as scratch:
                with tarfile.open(fileobj=_QueueWriter(q), mode="w|gz") as tar:
                    for root, prefix in sources:
                        _add_tree(tar, root, prefix, Path(scratch), counter)
                        status["files"] = counter["files"]
        except Exception as error:
            status["error"] = f"{type(error).__name__}: {error}"
        finally:
            q.put(None)

    threading.Thread(target=produce, daemon=True).start()
    buffer = b""
    while True:
        piece = q.get()
        if piece is not None:
            buffer += piece
        while len(buffer) >= FRAME or (piece is None and buffer):
            chunk, buffer = buffer[:FRAME], buffer[FRAME:]
            sealed = aes.encrypt(_nonce(n), chunk, None)
            n += 1
            status["bytes"] = status.get("bytes", 0) + len(chunk)
            yield struct.pack(">I", len(sealed)) + sealed
        if piece is None:
            break
    if status.get("error"):
        raise RuntimeError(status["error"])
    status["files"], status["skipped"] = counter["files"], counter.get("skipped", [])
    sealed = aes.encrypt(_nonce(n), END + struct.pack(">Q", counter["files"]), None)
    yield struct.pack(">I", len(sealed)) + sealed + struct.pack(">I", 0)


def default_sources() -> list[tuple[Path, str]]:
    import sandbox
    import store
    data_root = Path(os.environ.get("HUB_ROOT_DATA_DIR") or store.DATA.parent)
    return [(data_root, "data"), (sandbox.ROOT, "cowork")]


def start_send(target: str, secret: str) -> dict:
    if SEND.get("state") == "sending":
        return SEND
    SEND.clear()
    SEND.update(state="sending", target=target, started=time.time(), bytes=0, files=0)

    def run():
        try:
            try:
                import httpx2 as httpx
            except ImportError:
                import httpx
            with httpx.Client(timeout=httpx.Timeout(60, read=900)) as client:
                response = client.post(target.rstrip("/") + "/restore", content=frames(secret, default_sources(), SEND))
            if response.status_code != 200:
                raise RuntimeError(f"receiver answered HTTP {response.status_code}: {response.text[:300]}")
            SEND.update(state="done", receiver=response.json(), finished=time.time())
        except Exception as error:
            SEND.update(state="failed", error=f"{type(error).__name__}: {error}"[:600], finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return SEND


# ---------------------------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------------------------
class _ChunkedReader:
    """Reads an HTTP/1.1 body (chunked or with Content-Length) as a stream."""
    def __init__(self, rfile, chunked: bool, length: int | None):
        self.rfile, self.chunked, self.left, self.buffer, self.done = rfile, chunked, length, b"", False

    def _fill(self):
        if self.done:
            return
        if not self.chunked:
            size = min(FRAME, self.left or 0)
            data = self.rfile.read(size) if size else b""
            self.left = (self.left or 0) - len(data)
            if not data:
                self.done = True
            self.buffer += data
            return
        line = self.rfile.readline().strip()
        size = int(line.split(b";")[0] or b"0", 16)
        if size == 0:
            self.rfile.readline()
            self.done = True
            return
        self.buffer += self.rfile.read(size)
        self.rfile.readline()

    def read(self, n: int) -> bytes:
        while len(self.buffer) < n and not self.done:
            self._fill()
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data


class _PipeReader(io.RawIOBase):
    def __init__(self, q: queue.Queue):
        self.q, self.buffer, self.finished = q, b"", False

    def readable(self):
        return True

    def readinto(self, target):
        while not self.buffer and not self.finished:
            piece = self.q.get()
            if piece is None:
                self.finished = True
            else:
                self.buffer = piece
        n = min(len(target), len(self.buffer))
        target[:n], self.buffer = self.buffer[:n], self.buffer[n:]
        return n


def receive(port: int, secret: str, data_dir: Path, cowork_dir: Path, once: bool = True) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    aes = AESGCM(_key(secret))
    state = {"state": "waiting", "bytes": 0}
    targets = {"data": data_dir, "cowork": cowork_dir}
    incoming = data_dir.parent / "restore-incoming"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, code: int, body: str):
            data = body.encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            import json
            self._reply(200, json.dumps(state))

        def do_POST(self):
            import json
            if self.path != "/restore" or state["state"] in {"receiving", "done"}:
                return self._reply(409, '{"error":"not accepting"}')
            state["state"] = "receiving"
            body = _ChunkedReader(self.rfile, "chunked" in (self.headers.get("transfer-encoding") or "").lower(),
                                  int(self.headers["content-length"]) if self.headers.get("content-length") else None)
            shutil.rmtree(incoming, ignore_errors=True)
            incoming.mkdir(parents=True)
            q: queue.Queue = queue.Queue(maxsize=64)
            result: dict = {}

            def extract():
                try:
                    with tarfile.open(fileobj=io.BufferedReader(_PipeReader(q), FRAME), mode="r|gz") as tar:
                        for member in tar:
                            top = member.name.split("/", 1)[0]
                            if top not in targets or member.name.startswith("/") or ".." in Path(member.name).parts:
                                continue
                            tar.extract(member, incoming, numeric_owner=True, filter="tar")
                            result["files"] = result.get("files", 0) + int(member.isfile())
                except Exception as error:
                    result["error"] = f"{type(error).__name__}: {error}"

            worker = threading.Thread(target=extract, daemon=True)
            worker.start()
            n, finished, error = 0, None, None
            try:
                while True:
                    header = body.read(4)
                    if len(header) < 4:
                        raise ValueError("stream ended early")
                    size = struct.unpack(">I", header)[0]
                    if size == 0:
                        break
                    plain = aes.decrypt(_nonce(n), body.read(size), None)
                    n += 1
                    if plain.startswith(END):
                        finished = struct.unpack(">Q", plain[len(END):])[0]
                        continue
                    state["bytes"] += len(plain)
                    while True:  # stop feeding if extraction has failed, instead of blocking on a full queue
                        if "error" in result:
                            raise ValueError(result["error"])
                        try:
                            q.put(plain, timeout=1)
                            break
                        except queue.Full:
                            continue
            except (InvalidTag, ValueError, struct.error) as problem:
                error = f"bad stream: {type(problem).__name__}: {problem}"
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
            worker.join(timeout=30)
            error = error or result.get("error") or (None if finished is not None else "no end marker")
            if error:
                state.update(state="failed", error=error)
                shutil.rmtree(incoming, ignore_errors=True)
                self.close_connection = True
                self._reply(400, json.dumps({"error": error}))
                state["state"] = "waiting"
                return
            for top, target in targets.items():
                source = incoming / top
                if not source.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    backup = target.with_name(target.name + f".before-restore-{int(time.time())}")
                    target.rename(backup)
                source.rename(target)
            shutil.rmtree(incoming, ignore_errors=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / ".restored").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            state.update(state="done", files=result.get("files", 0), sent_files=finished)
            self._reply(200, json.dumps({"files": result.get("files", 0), "sender_files": finished, "bytes": state["bytes"]}))
            if once:
                threading.Thread(target=server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[restore] waiting for data on port {port}", flush=True)
    server.serve_forever()
    server.server_close()
    return state


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("receive")
    r.add_argument("--port", type=int, default=int(os.environ.get("RESTORE_PORT", "9000")))
    r.add_argument("--data", default=os.environ.get("DATA_DIR", "/workspace/data"))
    r.add_argument("--cowork", default=os.environ.get("COWORK_ROOT", "/workspace/cowork"))
    args = ap.parse_args()
    secret = os.environ.get("RESTORE_KEY", "")
    if len(secret) < 32:
        sys.exit("RESTORE_KEY must be at least 32 characters")
    state = receive(args.port, secret, Path(args.data), Path(args.cowork))
    print(f"[restore] {state}", flush=True)


if __name__ == "__main__":
    main()
