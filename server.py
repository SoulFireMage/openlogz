#!/usr/bin/env python3
"""Serve the reader from a Hugging Face Space with the bucket mounted at DATA_DIR.

    GET  /          the reader, rebuilt when files in the bucket change
    GET  /refresh   rebuild now
    POST /import    body: the chat app's Export JSON; saved to imports/
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import openlogz

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PORT = int(os.environ.get("PORT", "7860"))
MAX_IMPORT = 512 * 1024 * 1024
RECHECK_SECONDS = 30

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
        if path not in ("/", "/refresh"):
            return self._send(404, "not found", "text/plain")
        if not DATA_DIR.is_dir():
            return self._send(200, "<p>No bucket mounted. In the Space settings, attach "
                                   "the logs bucket at <code>/data</code>.</p>")
        self._send(200, page(force=path == "/refresh"))

    def do_POST(self):
        if self.path.split("?")[0] != "/import":
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
