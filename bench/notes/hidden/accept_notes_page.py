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


class Page(Base):
    def test_list(self):
        """T-003: `GET /` shows every note's text in its own `<li>`, oldest first, with HTML in a note escaped (`<b>` shows as `&lt;b&gt;`)."""
        self.add("first note")
        self.add("<b>bold</b>")
        status, _, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("<b>bold", page)
        items = [chunk.split("</li>")[0].strip() for chunk in page.split("<li")[1:]]
        self.assertEqual(len(items), 2, page)
        self.assertIn("first note", items[0])
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", items[1])

    def test_form(self):
        """T-003: the page has a `<form method="post" action="/notes">` with a text field named `text`."""
        page = self.call("GET", "/")[2]
        self.assertRegex(page, r'<form[^>]*method="post"[^>]*action="/notes"|<form[^>]*action="/notes"[^>]*method="post"')
        self.assertRegex(page, r'name="text"')

    def test_form_post(self):
        """T-003: a form post to `/notes` (`text=...`, form-encoded) adds the note and answers 303 with `Location: /`; an empty one adds nothing and still answers 303."""
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        status, headers, _ = self.call("POST", "/notes", "text=from+the+form%21", form)
        self.assertEqual((status, headers.get("Location")), (303, "/"))
        self.assertEqual(self.json("GET", "/api/notes")[1], [{"id": 1, "text": "from the form!"}])
        self.assertEqual(self.call("POST", "/notes", "text=", form)[0], 303)
        self.assertEqual(len(self.json("GET", "/api/notes")[1]), 1)
