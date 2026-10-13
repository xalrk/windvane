# Security policy

## Reporting a vulnerability
Please don't report security problems in a public issue. Use GitHub's private reporting instead: the repository's
**Security** tab, then **Report a vulnerability**. Only the maintainers see the report.

It helps to include what you ran, what happened, and what an attacker could do with it. We'll reply as soon as we
can, keep you posted while we work on a fix, and credit you in the release notes unless you'd rather we didn't.

## Supported versions
windvane is a rolling release: fixes go into the next release, and only the latest release is supported. Please
check that the problem still happens on it (or on `main`) before you report.

## What counts
windvane runs Claude Code sessions unattended with push access to your repository, so its safety rests on the
limits in [docs/safety.md](docs/safety.md). We especially want to hear about:
- a session doing something its role may not: pushing to `main`, editing files outside its worktree, running
  commands its rules deny, or getting around the critic, the check or the reviewer;
- the driver changing your own checkout's branch, index or files while `isolate` is on;
- a token, email address or ntfy topic ending up in a commit, pull request, push, log or phone notification;
- content a session reads (a ticket, a file in the repository, a pull request, a phone answer) making windvane run
  commands or change settings it shouldn't;
- `install.sh` or the self-update installing something other than the windvane release or commit it names.

Problems in Claude Code itself, in `gh` or in ntfy belong with those projects. A session that does something its
configured permissions allow isn't a vulnerability in windvane, but if a default seems unsafe, an issue is welcome.
