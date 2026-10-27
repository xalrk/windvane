# What a user of recipes meets

Each check is answered by running `python3 recipes.py` in a scratch folder of your own (`$TMPDIR`), on recipe files
written there in the form `CLAUDE.md` gives.

- J1: `python3 recipes.py --help` lists `show`, `scale` and `shop`, and each command's `--help` names its arguments and says what they are for.
- J2: no command prints the help; an unknown command (`recipes.py bake x.txt`) and a command missing its file (`show` alone) each print a usage line to stderr and exit 2, with no traceback.
- J3: an ingredient name with non-ASCII letters (`200 g crème fraîche`) and a title with them come back from `show` exactly as typed.
- J4: `show` on `examples/pancakes.txt` prints its title, `serves 4` and every ingredient, with `1 1/2 cup milk` and `1/2 tsp salt` as fractions.
- J5: every error the tickets name was made to happen and printed on one line starting `error: `, with the exit code `CLAUDE.md` gives (usage 2, others 1), and a bad line's error names the file and the right line number.
- J6: scaling a recipe `--by 3` and then `--by 1/3` by hand gives back the quantities it started with, with no `0.99` or `1.01` creeping in.
- J7: no command changes a recipe file: its bytes and modification time are the same after `show`, `scale` and `shop`.
- J8: `python3 -m unittest discover -s tests` passes, in under a minute.
