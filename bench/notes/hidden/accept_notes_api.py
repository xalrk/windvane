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


class Api(Base):
    def test_add(self):
        """T-001: `POST /api/notes` with `{"text": "buy milk"}` answers 201 with `{"id": 1, "text": "buy milk"}`; the next note gets id 2."""
        self.assertEqual(self.json("POST", "/api/notes", {"text": "buy milk"}), (201, {"id": 1, "text": "buy milk"}))
        self.assertEqual(self.add("call mum")["id"], 2)

    def test_list(self):
        """T-001: `GET /api/notes` answers 200 with a JSON list of every note, oldest first, each `{"id": <id>, "text": <text>}`."""
        self.assertEqual(self.json("GET", "/api/notes"), (200, []))
        self.add("one")
        self.add("two")
        self.assertEqual(self.json("GET", "/api/notes"), (200, [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]))

    def test_bad_text(self):
        """T-001: a note whose `text` is missing, not a string, or only spaces answers 400 with `{"error": <message>}` and adds nothing."""
        for body in ({}, {"text": 5}, {"text": "   "}):
            status, got = self.json("POST", "/api/notes", body)
            self.assertEqual(status, 400, body)
            self.assertIsInstance(got.get("error"), str)
        self.assertEqual(self.json("GET", "/api/notes"), (200, []))
