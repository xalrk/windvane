# What a player of maze meets

Each check is answered by playing `python3 game.py` on the maps in `maps/` (and maps you write in `$TMPDIR`).

- J1: `python3 game.py` with no map, and with a map file that doesn't exist, each prints one line starting `error: ` to stderr and exits non-zero, with no traceback.
- J2: an unknown command (`jump`), an empty line and a command in capitals (`N`) each get at most one line, and the game goes on from the same square.
- J3: a player can find every command the tickets built without reading the code: `--help`, a `help` command or a README in the project lists them.
- J4: a whole game played by hand on `maps/first.txt` prints only lines the tickets name: no prompt, no debug output, no blank lines.
- J5: the same commands on the same map, run twice, give byte-identical output.
- J6: the input ending at any point of a game (stdin closed after any command) ends it with no traceback.
- J7: `python3 -m unittest discover -s tests` passes, in under a minute.
- J8: each sentence the tickets name (`You can't go that way.`, `The door is locked.` ...) is written in one place in the code: `grep -rn` finds it once outside `tests/`.
