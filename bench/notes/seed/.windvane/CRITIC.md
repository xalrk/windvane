# What a 10 means for this project

One person's notes in a browser tab, kept in a file they own. A 10 is as dependable as a well-made self-hosted app:
nothing typed is ever lost, the page works without JavaScript, and the API answers every request, even a wrong one,
with a status and a JSON message a script can act on.

## works
What the ticket promises, tried in a browser or with curl against the running server.
- 2 runs: started as a user would, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes the server, hangs a request or loses a note.
- 7 failures: each failure the ticket names (bad input, an unknown id, a missing route) was made to happen and answered with the right status and a message saying what is wrong.
- 8 state: the data file is right on every way out: an error, a restart, two requests at once.
- 10 class: the change removes a whole class of failure (an unescaped note, a half-written file) rather than one case of it.

## screens
What a user sees on the page.
- 3 flow: on the page, a user can do the ticket's task start to finish.
- 7 states: the page reads right with no notes, many notes and a note with HTML in it.
- 9 plain: it is readable at phone and desktop width, with nothing broken or unstyled-looking.
