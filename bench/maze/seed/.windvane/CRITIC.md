# What a 10 means for this project

Players who like short puzzle games in a terminal, and people who script them. A 10 plays as cleanly as a classic
text adventure (Zork's parser, a roguelike's map): every command answered in plain words, the same game every time,
and nothing a player types or saves ever crashing it or losing their place.

## works
What the ticket promises, played the way a player plays it.
- 2 plays: a game played by hand on the map in `maps/`, the ticket's main promise does what it says.
- 5 safe: nothing typed (a typo, an empty line, the input ending) crashes, hangs or prints a traceback.
- 7 failures: each failure the ticket names (a wall, a locked door, a bad file) was made to happen and answered in one plain sentence.
- 8 state: position, keys, doors and moves stay right on every way out: a refused move, a save and load, the input ending.
- 10 class: the same script on the same map always gives the same output, byte for byte, across the whole game.

## reads
What a player reads.
- 3 plain: every line is one sentence in the ticket's exact words; nothing else is printed.
- 7 map: `look` shows the map as it is now, readable at a glance.
- 9 consistent: the same thing is always said the same way, in every command.
