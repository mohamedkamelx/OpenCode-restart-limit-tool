"""Self-contained client for the Huawei DG8045 gateway at 192.168.1.1.

No external modules: legacy-TLS HTTP over a single keep-alive socket, RSA
password encryption, CSRF handling, and the handful of API calls the
autocontinue tool needs.

Credentials come from ROUTER_USER / ROUTER_PASS, defaulting to the values the
gateway was set up with.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import ssl

HOST = os.environ.get("ROUTER_HOST", "192.168.1.1")
PORT = int(os.environ.get("ROUTER_PORT", "443"))

# Keep the failure fast when the folder is used on someone else's network.
CONNECT_TIMEOUT = float(os.environ.get("ROUTER_CONNECT_TIMEOUT", "3"))

USER = os.environ.get("ROUTER_USER", "vodafone")
PASS = os.environ.get("ROUTER_PASS", "NHBWA5UF")

WRAPPER = "while(1); /*"


def _ctx() -> ssl.SSLContext:
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    c.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        c.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        pass
    return c


class Conn:
    """One persistent TLS connection; the router ties the login to it."""

    def __init__(self, timeout: float = 15.0):
        raw = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT)
        raw.settimeout(timeout)
        self.s = _ctx().wrap_socket(raw, server_hostname=HOST)
        self.buf = b""
        self.cookie = ""

    def req(self, method: str, path: str, body: bytes = b"",
            ctype: str | None = None, referer: str = "/"):
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {HOST}",
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36",
            "Accept: */*",
            "Accept-Language: en-US,en;q=0.9",
            f"Referer: https://{HOST}{referer}",
            "Connection: keep-alive",
        ]
        if self.cookie:
            lines.append(f"Cookie: {self.cookie}")
        if ctype:
            lines.append(f"Content-Type: {ctype}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        self.s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        return self.read_response()

    def _fill(self):
        data = self.s.recv(65536)
        if not data:
            raise EOFError("router closed the connection")
        self.buf += data

    def read_response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0]
        hdrs = {}
        for line in lines[1:]:
            if ":" in line:
                key, val = line.split(":", 1)
                hdrs[key.strip().lower()] = val.strip()
        for line in lines[1:]:
            if line.lower().startswith("set-cookie:"):
                pair = line.split(":", 1)[1].strip().split(";", 1)[0]
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    self.cookie = f"{key.strip()}={val.strip()}"
        n = int(hdrs.get("content-length", "0") or 0)
        while len(self.buf) < n:
            self._fill()
        body = self.buf[:n]
        self.buf = self.buf[n:]
        return status, hdrs, body


def meta_of(html: str) -> dict:
    meta = {}
    for tag in re.findall(r"<meta[^>]*>", html, re.I):
        name = re.search(r'name="([^"]+)"', tag)
        content = re.search(r'content="([^"]*)"', tag)
        if name and content:
            meta[name.group(1)] = content.group(1)
    return meta


def rsa_hex(msg: bytes, n_hex: str, e_hex: str) -> str:
    """PKCS#1 v1.5 type-2 padding, as the gateway's login page does."""
    n = int(n_hex, 16)
    e = int(e_hex, 16)
    k = (n.bit_length() + 7) // 8
    ps = bytes(secrets.choice(range(1, 256)) for _ in range(k - len(msg) - 3))
    m = int.from_bytes(b"\x00\x02" + ps + b"\x00" + msg, "big")
    return format(pow(m, e, n), "x").rjust(k * 2, "0")


def strip_wrapper(text: str) -> str:
    text = text.strip()
    if text.startswith(WRAPPER):
        text = text[len(WRAPPER):]
    if text.endswith("*/"):
        text = text[:-2]
    return text.strip()


class Session:
    """Logged-in API session against the gateway."""

    def __init__(self):
        self.c = Conn()
        self.csrf = {}
        self.login_result = {}
        self.login()

    def login(self):
        st, h, body = self.c.req("GET", "/")
        meta = meta_of(body.decode("utf-8", "replace"))
        enc = rsa_hex(base64.b64encode(PASS.encode()), meta["n"], meta["e"])
        payload = json.dumps({
            "csrf": {"csrf_param": meta["csrf_param"],
                     "csrf_token": meta["csrf_token"]},
            "data": {"UserName": USER, "Password": enc},
        }).encode()
        st, h, body = self.c.req("POST", "/api/system/user_login", payload,
                                 "application/json;charset=UTF-8;enp", "/")
        self.login_result = json.loads(
            strip_wrapper(body.decode("utf-8", "replace")))
        self._sync_csrf(self.login_result)
        return self.login_result

    def _sync_csrf(self, obj):
        if isinstance(obj, dict) and obj.get("csrf_param") and obj.get("csrf_token"):
            self.csrf = {"csrf_param": obj["csrf_param"],
                         "csrf_token": obj["csrf_token"]}

    def get(self, path: str, referer: str = "/"):
        return self.c.req("GET", path, referer=referer)

    def get_text(self, path: str, referer: str = "/"):
        st, h, body = self.get(path, referer)
        return st, h, body.decode("utf-8", "replace")

    def api(self, objname: str):
        st, h, body = self.get("/api/" + objname, referer="/")
        return st, h, strip_wrapper(body.decode("utf-8", "replace"))

    def post_api(self, objname: str, obj: dict):
        """POST /api/<objname>, attaching and refreshing the CSRF pair."""
        payload = {"csrf": self.csrf}
        payload.update(obj)
        body = json.dumps(payload).encode()
        st, h, raw = self.c.req("POST", "/api/" + objname, body,
                                "application/json;charset=UTF-8;enp", "/")
        text = strip_wrapper(raw.decode("utf-8", "replace"))
        try:
            data = json.loads(text)
        except ValueError:
            return st, text
        self._sync_csrf(data)
        return st, data
