"""notes: a small notes web app, served by http.server."""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Notes</title></head>
<body><h1>Notes</h1></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, kind="application/json"):
        data = (json.dumps(body) if kind == "application/json" else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, PAGE, "text/html")
        if self.path == "/health":
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="notes")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="notes.json", help="the file the notes are kept in")
    a = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.data = a.data
    print(f"listening on http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
