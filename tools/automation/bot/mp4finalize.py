"""Bounds-checked QuickTime sample-entry and metadata finalization."""
from pathlib import Path
import struct


def resolve_reference(path):
    path = Path(path).expanduser()
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, *(p / path for p in Path(__file__).resolve().parents)]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"reference MOV not found: {path}")


def boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    while start < end:
        if start + 8 > end:
            raise ValueError("truncated box header")
        size, kind = struct.unpack_from(">I4s", buf, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError("truncated extended box")
            size = struct.unpack_from(">Q", buf, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("invalid box size")
        yield start, start + size, kind, start + header
        start += size


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def metadata_value(buf, start, end, value):
    kind = int.from_bytes(buf[start:start+4], "big") & 0xffffff
    if kind in (21, 22):
        width = end-start-8
        # ffprobe exposes the first 32 bits of the reference's 8-byte integer.
        # Preserve its trailing bytes and the exact default meta box.
        used = min(width, 4)
        return int(value).to_bytes(used, "big", signed=kind == 21) + buf[start+8+used:end]
    if kind == 1:
        return str(value).encode("utf-8")
    raise ValueError(f"unsupported metadata data type: {kind}")


def reference_meta(reference, values):
    buf = Path(reference).read_bytes()
    for _, end, kind, body in boxes(buf):
        if kind != b"moov":
            continue
        for off, stop, typ, payload in boxes(buf, body, end):
            if typ != b"meta":
                continue
            children = list(boxes(buf, payload, stop))
            keys = {}
            for _, ke, kt, kb in children:
                if kt == b"keys":
                    count = struct.unpack_from(">I", buf, kb + 4)[0]
                    entries = list(boxes(buf, kb + 8, ke))
                    if len(entries) != count:
                        raise ValueError("invalid metadata key count")
                    keys = {i: bytes(buf[b:e]).decode() for i, (_, e, _, b) in enumerate(entries, 1)}
            required = {"com.apple.quicktime." + k for k in (*values, "creationdate")}
            if not required.issubset(keys.values()):
                continue
            # Preserve the reference bytes exactly when values are unchanged.
            result = []
            for co, ce, ct, cb in children:
                if ct != b"ilst":
                    result.append(buf[co:ce]); continue
                items = []
                for io, ie, it, ib in boxes(buf, cb, ce):
                    key = keys.get(int.from_bytes(it, "big"), "").removeprefix("com.apple.quicktime.")
                    if key not in values:
                        items.append(buf[io:ie]); continue
                    data = list(boxes(buf, ib, ie))
                    items.append(box(it, b"".join(box(dt, buf[db:db+8] + metadata_value(buf, db, de, values[key])) if dt == b"data" else buf[do:de] for do, de, dt, db in data)))
                result.append(box(b"ilst", b"".join(items)))
            return box(b"meta", b"".join(result))
    raise ValueError("reference has no QuickTime metadata with required keys")


def finalize(target, reference, values):
    # When reference is None the /moov/meta append is skipped; the four
    # com.apple.quicktime.* keys must then come from ffmpeg -metadata.
    # The FFMP-zero, mdhd-language and ftyp-minor_version patches always run.
    meta = reference_meta(reference, values) if reference is not None else b""
    buf = bytearray(Path(target).read_bytes())
    patched = 0

    def walk(start, end):
        nonlocal patched
        for off, stop, kind, body in boxes(buf, start, end):
            if kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                walk(body, stop)
            elif kind == b"stsd":
                for eo, ee, et, eb in boxes(buf, body + 8, stop):
                    if et == b"avc1":
                        # VisualSampleEntry vendor field: type offset + 16.
                        vendor = eb + 12
                        if vendor + 4 > ee:
                            raise ValueError("truncated avc1 sample entry")
                        buf[vendor:vendor+4] = b"\0" * 4
                        patched += 1
            elif kind == b"mdhd":
                # MOV muxer maps und to 0x7fff; the reference uses ISO-639 packed und.
                version = buf[body]
                if version not in (0, 1):
                    raise ValueError("unsupported mdhd version")
                language = body + (20 if version == 0 else 32)
                if language + 2 > stop:
                    raise ValueError("truncated mdhd")
                struct.pack_into(">H", buf, language, 0x55c4)
            elif kind == b"ftyp":
                if body + 8 > stop:
                    raise ValueError("truncated ftyp")
                buf[body+4:body+8] = b"\0" * 4

    walk(0, len(buf))
    if not patched:
        raise ValueError("no avc1 sample entry")
    # Normalize zero-sized terminal boxes before appending metadata.
    for off, end, _, _ in boxes(buf):
        if buf[off:off+4] == b"\0" * 4:
            struct.pack_into(">I", buf, off, end-off)
    Path(target).write_bytes(buf + meta)
