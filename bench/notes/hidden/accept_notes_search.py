import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

APP = Path("app.py").resolve()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.data = self.dir / "notes.json"
        self.start()

    def start(self):
        self.proc = subprocess.Popen([sys.executable, str(APP), "--port", "0", "--data", str(self.data)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=self.dir)
        self.addCleanup(self.stop)
        self.port = urlsplit(self.proc.stdout.readline().split()[-1]).port

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        self.proc.stdout.close()

    def restart(self):
        self.stop()
        self.start()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        if isinstance(body, (dict, list)):
            body, headers = json.dumps(body), {"Content-Type": "application/json", **(headers or {})}
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r.status, dict(r.getheaders()), data

    def json(self, method, path, body=None):
        status, _, data = self.call(method, path, body)
        return status, json.loads(data) if data else None

    def add(self, text):
        status, note = self.json("POST", "/api/notes", {"text": text})
        self.assertEqual(status, 201)
        return note


class Search(Base):
    def test_q(self):
        """T-004: `GET /api/notes?q=<words>` lists only the notes whose text contains `<words>`, ignoring case."""
        self.add("Buy milk")
        self.add("call mum")
        self.add("more MILK")
        self.assertEqual([n["text"] for n in self.json("GET", "/api/notes?q=milk")[1]], ["Buy milk", "more MILK"])
        self.assertEqual(self.json("GET", "/api/notes?q=call%20mum")[1], [{"id": 2, "text": "call mum"}])
        self.assertEqual(self.json("GET", "/api/notes?q=nothing")[1], [])

    def test_bad_json(self):
        """T-004: a body that isn't JSON, or isn't a JSON object, on `POST /api/notes` answers 400 with `{"error": <message>}`."""
        for body in ("{not json", '["a list"]', '"text"'):
            status, _, data = self.call("POST", "/api/notes", body, {"Content-Type": "application/json"})
            self.assertEqual(status, 400, body)
            self.assertIsInstance(json.loads(data).get("error"), str)
        self.assertEqual(self.json("GET", "/api/notes"), (200, []))

    def test_unknown_api(self):
        """T-004: any other path under `/api/` answers 404 with `{"error": <message>}`, for GET, POST and DELETE alike."""
        for method in ("GET", "POST", "DELETE"):
            status, _, data = self.call(method, "/api/nothing", {"text": "x"} if method == "POST" else None)
            self.assertEqual(status, 404, method)
            self.assertIsInstance(json.loads(data).get("error"), str)
