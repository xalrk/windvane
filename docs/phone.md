# Phone setup

windvane can push progress to your phone and take answers back through [ntfy](https://ntfy.sh), a free, open source
push service with Android and iOS apps. It is **off by default**: without it everything works, and questions wait for
`windvane answer` at the desk.

## Set it up

```sh
windvane phone setup                 # on ntfy.sh
windvane phone setup --server https://ntfy.example.com   # or on your own ntfy server
```

This:
1. creates two random 32-character topics, one for pushes and one for the phone's replies, and saves them in
   `~/.config/windvane/phone.toml` (readable only by you);
2. sends a test push;
3. shows the topic to subscribe to, as text and, when `qrencode` is installed, as a QR code;
4. installs `windvane phone listen` as a systemd user service, so the phone's commands are answered even when no run
   is going. `--no-service` skips it; a running autopilot reads the replies itself anyway.

On the phone, install the ntfy app, tap **+**, and subscribe to the topic shown (for your own server, choose "Use
another server" first).

**Keep the topic private.** On ntfy.sh the topic name is the only thing keeping others out: anyone who has it can
read your pushes. windvane never writes the topics into logs, pushes, commits or pull requests. If one leaks,
`windvane phone rotate` makes new ones (subscribe to the new topic afterwards).

`windvane phone off` turns the phone off again; `windvane doctor` shows whether it is on and working.

## What you get

A push for each merge, failure, pause and question, and a summary when the run stops. Every title and question
starts with the project's `display_name` (default: its `name` with a capital, `acme` -> `Acme`), so two projects on
one phone stay apart:

```
Acme windvane started
Until 04:10. 5h 5% · week 9%.

Acme: Done: Phone notifications that work for anyone (T-015)
Built, checked, reviewed and merged. Critic 7/10. 5h 5% · week 9%

Acme: paused
5h 75% · week 40%. Resuming at 14:00.

Acme windvane stopped
Why: deadline.
Merged: Phone notifications that work for anyone (T-015).
Waiting on you: none.
Failed: none.
Set aside: Retry with backoff (T-016) (hit its max turns).
5h 31% · week 12%.
```

Every push names its ticket by the whole title and leads with what happened, then the next step when there is one, in
reading order: an error or other free text goes in whole, as a sentence, and ntfy wraps it. Only free text past 1,500
characters is cut, at a sentence or line end, and the project log keeps all of it.

A **Set aside** push says which limit a ticket's run hit and where to change it: the `ticket_hours` time box, its
max turns (windvane sets none, so it's the Claude CLI's default or the project's Claude settings in `.claude/`), or its
dollar budget (windvane sets none either and has no setting for one, so that push only says to split the ticket). The summary lists every set-aside ticket with why, and on its own line the tickets stopped at the
deadline mid-run, which the next run resumes. A `setup` that fails is quoted up to 60 characters; a deadline reached during setup reads
"deadline reached during setup; nothing ran".

A **question** push carries buttons for options A and B and **Answer on computer**. Tap a letter to answer; options
C to E are typed as the letter. "Answer on computer" tells the run to leave the question for `windvane answer`
without waiting for it. With no answer in `answer_wait` minutes (default 10), the run builds the recommended option
and marks it provisional, so you can change it later.

## Commands from the phone

Type one of these into the topic in the ntfy app:

| Command | Reply |
|---|---|
| `status` | one line per project whose autopilot is on (`Acme: autopilot running. Last: ...`); idle ones are left out |
| `overview` | every project, running or not: tickets merged, underway, not started |
| `usage` | Claude usage: the 5-hour window and the week |
| `stop` | stop the runs after their current ticket |
| `answer <id> B` | answer a question by its id |
| `B` | answer the newest open question with B |
| `later <id>` | leave a question for the computer |
| `help` | this list |

**Nothing else sent from the phone does anything.** Whoever learns the reply topic can post to it, so windvane never
treats a message as instructions: it accepts only these fixed commands, and a letter only chooses among options
windvane itself wrote.

## Privacy

Every push is censored before it leaves the machine: email addresses, tokens and keys, passwords, your user name
and git name and email, the topics themselves, and any extra words you list under `redact` in `phone.toml`
(host names, client names, project names):

```toml
redact = ["acme-internal", "my-laptop"]
```

If the censor fails for any reason, nothing is sent.

## Using your own notification scripts

If commands named `notify` and `notify-ask` are on your `PATH`, windvane uses them instead of the built-in phone,
and `ntfy-listen` for replies. `windvane init` then adds `stop` and `overview` hooks for the project and one
`status` hook for every project (`status.d/windvane`) under `~/.config/ntfy/`; a run's start removes the hooks of a
project whose repo is gone. The built-in phone stays as the fallback.
