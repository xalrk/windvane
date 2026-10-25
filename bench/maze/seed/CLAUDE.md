# maze

A small text maze game: find the key, open the door, get out. The engine is deterministic: the same map and the
same commands always give the same output, so a game can be scripted, replayed and tested.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `game.py`, run as `python3 game.py <map file>`; it reads one command per line from stdin and prints
  what happens, one sentence per line, with no prompt. It starts by printing `You are at (x, y).`
- Map files are text: `#` wall, `.` floor, `@` start, `k` key, `D` locked door, `E` exit. `x` is the column and `y`
  the row, both counted from 0 at the top left.
- Errors that stop the game go to stderr as one line starting `error: `, never a traceback.
- Every change comes with tests in `tests/` that play scripted games.

## Run and test
- `python3 game.py maps/first.txt`
- `python3 -m unittest discover -s tests`
