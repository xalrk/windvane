"""notes: a small notes web app, served by http.server (the bench's reference solution)."""
import argparse
import html
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Notes</title></head>
<body><h1>Notes</h1>
<form method="post" action="/notes"><input type="text" name="text"><button>Add</button></form>
<ul>{items}</ul></body></html>
"""


class Store:
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()
        d = json.loads(self.path.read_text()) if self.path.exists() else {"next": 1, "notes": []}
        self.next, self.notes = d["next"], d["notes"]

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"next": self.next, "notes": self.notes}))
        os.replace(tmp, self.path)

    def add(self, text):
        with self.lock:
            note = {"id": self.next, "text": text}
            self.notes.append(note)
            self.next += 1
            self.save()
            return note

    def delete(self, nid):
        with self.lock:
            left = [n for n in self.notes if n["id"] != nid]
            if len(left) == len(self.notes):
                return False
            self.notes = left
            self.save()
            return True


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body=None, kind="application/json", headers=()):
        data = b"" if body is None else (json.dumps(body) if kind == "application/json" else body).encode()
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, message):
        self.send(status, {"error": message})

    @property
    def store(self):
        return self.server.store

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/":
            items = "".join(f"<li>{html.escape(n['text'])}</li>" for n in self.store.notes)
            return self.send(200, PAGE.format(items=items), "text/html")
        if u.path == "/health":
            return self.send(200, {"ok": True})
        if u.path == "/api/notes":
            q = parse_qs(u.query).get("q", [""])[0].lower()
            return self.send(200, [n for n in self.store.notes if q in n["text"].lower()])
        self.error(404, "not found")

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/notes":
            text = parse_qs(self.body()).get("text", [""])[0]
            if text.strip():
                self.store.add(text)
            return self.send(303, headers=[("Location", "/")])
        if u.path != "/api/notes":
            return self.error(404, "not found")
        try:
            d = json.loads(self.body())
        except ValueError:
            return self.error(400, "the body isn't JSON")
        if not isinstance(d, dict):
            return self.error(400, "the body must be a JSON object")
        text = d.get("text")
        if not isinstance(text, str) or not text.strip():
            return self.error(400, "text must be a non-empty string")
        self.send(201, self.store.add(text))

    def do_DELETE(self):
        m = re.fullmatch(r"/api/notes/(\d+)", urlsplit(self.path).path)
        if not m:
            return self.error(404, "not found")
        if not self.store.delete(int(m.group(1))):
            return self.error(404, f"no note {m.group(1)}")
        self.send(204)

    def log_message(self, *args):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="notes")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="notes.json", help="the file the notes are kept in")
    a = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.store = Store(a.data)
    print(f"listening on http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
