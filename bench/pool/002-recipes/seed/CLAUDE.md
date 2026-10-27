# recipes

A small command-line recipe scaler: recipes kept as plain text files, shown, scaled to another number of servings
and summed into one shopping list from the shell. For a cook who keeps recipes in files they can edit, not an app.

## The recipe file
```
# Pancakes
serves 4

200 g flour
2 eggs
1 1/2 cup milk
1/2 tsp salt
```
The first line is `# <title>`, then `serves <N>`, then one ingredient per line: a quantity, an optional unit and the
ingredient's name. Blank lines are ignored. `examples/pancakes.txt` is one.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `recipes.py`, run as `python3 recipes.py <command> ...`. It never changes a recipe file.
- Errors go to stderr as one line starting `error: `, never a traceback; usage errors exit 2, other errors exit 1.
- Every change comes with tests in `tests/`.

## Run and test
- `python3 recipes.py --help`
- `python3 -m unittest discover -s tests`
