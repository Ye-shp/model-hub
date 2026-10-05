#!/usr/bin/env python3
"""
onion-fetch — pull data through the local Tor SOCKS5 proxy (127.0.0.1:9151).

Usage:
  python3 fetch.py URL [URL2 ...]          # save each page as <slug>.html in ./onion-data
  python3 fetch.py --json URL ...          # print JSON: {url, status, time, title, ...}
  python3 fetch.py --text URL              # print visible text only (no HTML tags)
  python3 fetch.py --retry N URL           # retry count (default 2, fresh circuit each time)
  python3 fetch.py --timeout N URL         # per-request timeout seconds (default 60)
  python3 fetch.py --out DIR URL ...       # output dir (default ./onion-data)

Every request uses a FRESH Tor circuit (SIGNAL NEWNYM) by default, so repeated
pulls look like different users.
"""
import json, os, re, socket, sys, time, ssl, urllib.request, urllib.error, select
from urllib.parse import urlsplit

SOCKS_HOST, SOCKS_PORT = "127.0.0.1", 9151
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = os.path.join(HERE, "run", "control_auth_cookie")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
SOCKS = "127.0.0.1:9151"


def _read_n(s, n):
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise ConnectionError("socks: connection closed")
        b += d
    return b


def socks_connect(host, port, proxy_host=SOCKS_HOST, proxy_port=SOCKS_PORT, timeout=60):
    """Open a TCP connection to host:port through the SOCKS5 proxy. Returns the socket."""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    s.sendall(bytes([5, 1, 0]))               # ver 5, 1 method, no-auth
    ver, method = _read_n(s, 2)
    if ver != 5:
        raise ConnectionError("socks: bad version")
    # send address
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):      # IPv4 literal
        octets = bytes(int(p) for p in host.split("."))
        s.sendall(bytes([5, 1, 1]) + octets + bytes([port >> 8, port & 255]))
    elif ":" in host:                                        # IPv6 literal
        import socket as _sk
        ip6 = _sk.inet_pton(_sk.AF_INET6, host)
        s.sendall(bytes([5, 1, 4]) + ip6 + bytes([port >> 8, port & 255]))
    else:                                                    # domain name
        addr = host.encode("idna")
        s.sendall(bytes([5, 1, 3, len(addr)]) + addr + bytes([port >> 8, port & 255]))
    resp = _read_n(s, 4)
    if resp[1] not in (0, 1):
        raise ConnectionError(f"socks: connect failed code {resp[1]}")
    atyp = resp[3]
    if atyp == 1:
        _read_n(s, 4)
    elif atyp == 3:
        _read_n(s, _read_n(s, 1)[0])
    else:  # 4 = IPv6
        _read_n(s, 16)
    _read_n(s, 2)                              # bound port
    return s


def new_circuit():
    try:
        s = socket.create_connection(("127.0.0.1", 9051), timeout=8)
        cookie = open(COOKIE, "rb").read().hex().upper()
        s.sendall(("AUTHENTICATE " + cookie + "\r\n").encode())
        s.settimeout(2.0); s.recv(1024)
        s.sendall(b"SIGNAL NEWNYM\r\n"); s.settimeout(2.0); s.recv(1024)
        s.close()
        return True
    except Exception as e:
        print(f"  (new-circuit: {e})", file=sys.stderr)
        return False


def circuit_ready(timeout=25):
    """Wait until Tor has an established circuit (new one after NEWNYM) before using it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", 9051), timeout=5)
            cookie = open(COOKIE, "rb").read().hex().upper()
            s.sendall(("AUTHENTICATE " + cookie + "\r\n").encode())
            s.settimeout(1.5); s.recv(1024)
            s.sendall(b"GETINFO status/circuit-established\r\n")
            s.settimeout(1.5)
            reply = b""
            try:
                # Multi-line control replies end with a final "NNN OK" line.
                while not (reply.endswith(b" OK\r\n") and b"\n" in reply[:-6]):
                    d = s.recv(1024)
                    if not d:
                        break
                    reply += d
            except Exception:
                pass
            s.close()
            txt = reply.decode(errors="replace")
            for ln in txt.splitlines():
                if "circuit-established=" in ln:
                    val = ln.split("=", 1)[1].strip()
                    if val.isdigit() and int(val) >= 1:
                        return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def fetch(url, timeout=60, rotate=True):
    """GET a URL through the local Tor SOCKS5 proxy, using curl as the HTTP/TLS
    client (curl's fingerprint is what many onion services accept)."""
    if rotate:
        new_circuit()
        circuit_ready()
    t0 = time.time()
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        body_path = tf.name
    try:
        proc = subprocess.run(
            ["curl", "-sL", "--socks5-hostname", SOCKS,
             "--max-time", str(int(timeout)),
             "-A", UA,
             "-w", "%{http_code}\t%{size_download}\t%{url_effective}\t%{time_total}",
             "-o", body_path, url],
            capture_output=True, text=True, timeout=timeout + 5)
        out = proc.stdout.strip()
        body = open(body_path, "rb").read()
    finally:
        try: os.unlink(body_path)
        except Exception: pass
    if not out:
        raise ConnectionError(f"curl failed: {proc.stderr.strip()[:200]}")
    parts = out.split("\t")
    status = int(parts[0]) if parts[0].isdigit() else 0
    final_url = parts[2] if len(parts) > 2 else url
    elapsed = float(parts[3]) if len(parts) > 3 and parts[3] else round(time.time() - t0, 2)
    ctype = ""
    return {"url": url, "status": status, "time": elapsed,
            "final_url": final_url, "content_type": ctype,
            "text": body.decode("utf-8", "replace")}


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<br[^>]*>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", text).strip()


def slug(url):
    h = re.sub(r"^https?://", "", url)
    return re.sub(r"[^a-z0-9]+", "_", h.lower())[:60] or "page"


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    as_text = "--text" in args
    retry, timeout = 2, 60
    out = "onion-data"
    def pop(flag, default):
        if flag in args:
            v = int(args[args.index(flag) + 1])
            args.remove(flag); args.remove(str(v)); return v
        return default
    retry = pop("--retry", retry)
    timeout = pop("--timeout", timeout)
    if "--out" in args:
        out = args[args.index("--out") + 1]; args.remove("--out"); args.remove(out)
    urls = [a for a in args if not a.startswith("--")]
    if not urls:
        print(__doc__); sys.exit(1)

    os.makedirs(out, exist_ok=True)
    ok = 0
    for url in urls:
        last = None
        for attempt in range(retry + 1):
            try:
                last = fetch(url, timeout=timeout, rotate=True)
                if last["status"] == 200 or last.get("text"):
                    break
                print(f"  retry {attempt + 1} for {url} (status {last['status']})...", file=sys.stderr)
            except Exception as e:
                last = {"url": url, "status": 0, "time": 0, "text": "", "error": repr(e)}
                print(f"  retry {attempt + 1} for {url} ({e!r})...", file=sys.stderr)
        if as_json:
            t = last.get("text", "")
            title = re.search(r"(?is)<title[^>]*>(.*?)</title>", t)
            d = {k: v for k, v in last.items() if k != "text"}
            d["title"] = title.group(1).strip() if title else ""
            d["bytes"] = len(t)
            print(json.dumps(d, indent=2))
        elif as_text:
            print(html_to_text(last.get("text", "")))
        else:
            path = os.path.join(out, slug(url) + ".html")
            open(path, "w").write(last.get("text", ""))
            print(f"[{last['status']}] {url} -> {path} ({last['time']}s, {len(last.get('text',''))} bytes)")
        ok += 1 if last["status"] < 400 else 0
    print(f"\n{ok}/{len(urls)} succeeded", file=sys.stderr)
    sys.exit(0 if ok == len(urls) else 2)


if __name__ == "__main__":
    main()
