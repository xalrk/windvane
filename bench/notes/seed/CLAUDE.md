# notes

A small notes web app: a page to write and read short notes, and a JSON API behind it, served by Python's own
`http.server`. For one person on their own machine who wants notes in a browser tab and a file they own.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `app.py`, run as `python3 app.py --port 8000 --data notes.json`. `--port 0` picks a free port; the
  first line it prints is always `listening on http://127.0.0.1:<port>/`, flushed, so a test or a script can find it.
- The JSON API answers JSON, errors included: `{"error": "<what is wrong>"}` with a 4xx status, never a traceback.
- Every change comes with tests in `tests/` that start the server on `--port 0` and talk to it over HTTP.

## Run and test
- `python3 app.py --port 8000 --data notes.json`, then open http://127.0.0.1:8000/
- `python3 -m unittest discover -s tests`
