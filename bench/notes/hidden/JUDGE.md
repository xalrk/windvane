# What a user of notes meets

Each check is answered by starting `python3 app.py --port 0 --data <file in $TMPDIR>` and talking to it with
`curl` or Python's `urllib`, then stopping it.

- J1: it prints `listening on http://127.0.0.1:<port>/` first, and Ctrl-C (SIGINT) stops it within two seconds with no traceback.
- J2: `--data` in a folder that doesn't exist, and a `--data` file that isn't JSON, each print one line starting `error: ` and exit non-zero, with no traceback.
- J3: `GET /` answers `Content-Type: text/html` with a charset, and the page has a `<title>`.
- J4: a note with non-ASCII text (`café ✓`) comes back the same through `GET /api/notes` and on the page.
- J5: every API error the tickets name was made to happen, and each `error` message names what is wrong (the field, the id or the path), never an empty string or a bare status word.
- J6: a note added through the form shows on the page the redirect leads to.
- J7: `python3 -m unittest discover -s tests` passes, in under a minute, and leaves no server running.
- J8: `python3 app.py --help` names `--port` and `--data` and says what each is for.
