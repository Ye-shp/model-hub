#!/usr/bin/env python3
"""Rotate Tor identity: SIGNAL NEWNYM (fresh circuits/exit node)."""
import socket, select, sys, time

def main():
    cookie_path = sys.argv[1] if len(sys.argv) > 1 else "tor/run/control_auth_cookie"
    cookie = open(cookie_path, "rb").read().hex().upper().encode()
    s = socket.create_connection(("127.0.0.1", 9051), timeout=10)
    s.setblocking(False)
    def rd(t=1.5):
        buf = b""
        end = time.time() + t
        while time.time() < end:
            if select.select([s], [], [], 0.2)[0]:
                d = s.recv(4096)
                if d: buf += d
                else: break
        return buf
    def cmd(c):
        s.sendall(c + b"\r\n"); time.sleep(0.3); return rd().decode(errors="replace").strip()
    print(cmd(b"AUTHENTICATE " + cookie))
    print(cmd(b"SIGNAL NEWNYM"))
    s.close()

if __name__ == "__main__":
    main()
