#!/usr/bin/env python3
"""Serve the reader from a Hugging Face Space with the bucket mounted at DATA_DIR.

    GET  /          the reader, rebuilt when files in the bucket change
    GET  /refresh   rebuild now
    POST /import    body: the chat app's Export JSON; saved to imports/
    GET/POST /login, GET /logout

A public or "protected" Space's app URL can be opened by anyone with the link.
Set the APP_PASSWORD secret to require a password for every page.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import openlogz

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PORT = int(os.environ.get("PORT", "7860"))
MAX_IMPORT = 512 * 1024 * 1024
RECHECK_SECONDS = 30
PASSWORD = os.environ.get("APP_PASSWORD", "")
COOKIE = "olz"
# Changing the password changes the token, which signs everyone out.
TOKEN = hmac.new(PASSWORD.encode(), b"openlogz-session-v1", hashlib.sha256).hexdigest()

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenLogz</title>
<style>body{font:16px system-ui,sans-serif;background:#f7f5f2;color:#1f1d1a;display:grid;
place-items:center;min-height:100vh;margin:0}form{display:grid;gap:10px;width:min(320px,90vw)}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #ccc}
button{background:#6d4bd8;color:#fff;border:0}p{color:#a33;margin:0}
@media (prefers-color-scheme:dark){body{background:#15141a;color:#e9e6f0}}</style></head>
<body><form method="post" action="login"><b>OpenLogz</b>%s
<input type="password" name="password" placeholder="Password" autofocus required>
<button>Open</button></form></body></html>"""

_lock = threading.Lock()
_cache: dict = {}
_state = {"html": None, "sig": None, "checked": 0.0}


def _signature():
    files = []
    for f in openlogz.iter_trace_files(DATA_DIR):
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((str(f), st.st_mtime, st.st_size))
    return hash(tuple(files))


def page(force=False) -> str:
    with _lock:
        now = time.time()
        if not force and _state["html"] and now - _state["checked"] < RECHECK_SECONDS:
            return _state["html"]
        _state["checked"] = now
        sig = _signature()
        if force or sig != _state["sig"] or not _state["html"]:
            gens, chats = openlogz.load_dir(DATA_DIR, _cache)
            _state["html"] = openlogz.render_html(openlogz.build_payload(gens, chats))
            _state["sig"] = sig
        return _state["html"]


class Handler(BaseHTTPRequestHandler):
    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def _redirect(self, where: str, cookie: str = ""):
        self.send_response(303)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _gate(self) -> bool:
        """True if the request may go on; otherwise answer it here."""
        if self._authed():
            return True
        if self.command == "POST":
            self._send(401, json.dumps({"error": "signed out"}), "application/json")
        else:
            self._redirect("login")
        return False

    def _send(self, code, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/login":
            return self._send(200, LOGIN_PAGE % "")
        if path == "/logout":
            return self._redirect("login", f"{COOKIE}=; Path=/; Max-Age=0; Secure; SameSite=None; Partitioned")
        if not self._gate():
            return
        if path not in ("/", "/refresh"):
            return self._send(404, "not found", "text/plain")
        if not DATA_DIR.is_dir():
            return self._send(200, "<p>No bucket mounted. In the Space settings, attach "
                                   "the logs bucket at <code>/data</code>.</p>")
        self._send(200, page(force=path == "/refresh"))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/login":
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            given = parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("password", [""])[0]
            if PASSWORD and hmac.compare_digest(given.encode(), PASSWORD.encode()):
                # SameSite=None + Partitioned so it also works inside the Hub's iframe
                return self._redirect(".", f"{COOKIE}={TOKEN}; Path=/; Max-Age=2592000; "
                                           "HttpOnly; Secure; SameSite=None; Partitioned")
            time.sleep(1)
            return self._send(401, LOGIN_PAGE % "<p>Wrong password.</p>")
        if not self._gate():
            return
        if path != "/import":
            return self._send(404, "not found", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_IMPORT:
            return self._send(413, json.dumps({"error": "empty or too large"}), "application/json")
        try:
            doc = json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send(400, json.dumps({"error": "not JSON"}), "application/json")
        if not isinstance(doc, dict) or doc.get("format") != openlogz.EXPORT_FORMAT:
            return self._send(400, json.dumps({"error": "not an OpenRouter Chat export"}),
                              "application/json")
        dest = DATA_DIR / "imports" / f"export-{time.strftime('%Y%m%d-%H%M%S')}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
        page(force=True)
        chats = len(openlogz.export_chats(doc))
        self._send(200, json.dumps({"saved": dest.name, "chats": chats}), "application/json")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"serving {DATA_DIR} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
