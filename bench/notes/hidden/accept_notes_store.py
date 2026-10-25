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


class Store(Base):
    def test_kept(self):
        """T-002: notes are kept in the `--data` file: after the server is stopped and started again on the same file, `GET /api/notes` lists the same notes and the next note gets the next id."""
        self.add("one")
        self.add("two")
        self.restart()
        self.assertEqual(self.json("GET", "/api/notes"), (200, [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]))
        self.assertEqual(self.add("three")["id"], 3)

    def test_delete(self):
        """T-002: `DELETE /api/notes/<id>` answers 204 and the note is gone from the list, also after a restart."""
        self.add("one")
        self.add("two")
        self.assertEqual(self.call("DELETE", "/api/notes/1")[0], 204)
        self.assertEqual(self.json("GET", "/api/notes"), (200, [{"id": 2, "text": "two"}]))
        self.restart()
        self.assertEqual(self.json("GET", "/api/notes"), (200, [{"id": 2, "text": "two"}]))

    def test_delete_unknown(self):
        """T-002: `DELETE` of an id there is no note for answers 404 with `{"error": <message>}`."""
        self.add("one")
        status, got = self.json("DELETE", "/api/notes/9")
        self.assertEqual(status, 404)
        self.assertIsInstance(got.get("error"), str)
        self.assertEqual(len(self.json("GET", "/api/notes")[1]), 1)
