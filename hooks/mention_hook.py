#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: when the user mentions windvane or autopilot, tell the session what that means
and to load the windvane skill, the way a keyword like "ultracode" switches a mode on. Installed by
`windvane install`. Silent for every other prompt, and after the first time in a session: the reminder is in the
context from then on, and background notices that quote the windvane log would otherwise repeat it on every turn."""
import json
import os
import re
import sys
from pathlib import Path

try:
    event = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
prompt, session = event.get("prompt", ""), re.sub(r"[^\w-]", "", str(event.get("session_id", "")))
seen = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "windvane-mentioned" / session if session else None
if seen and seen.exists():
    sys.exit(0)
if re.search(r"\bwind-?vane|\bauto-?pilot\b", prompt, re.I):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        "The user mentioned windvane / autopilot: their own Claude Code autopilot (command `windvane`)"
        ", which builds a project ticket by ticket in fresh headless sessions with a critic, a reviewer, "
        "phone questions and parallel slots. Load the `windvane` skill (Skill tool) before acting: it is the "
        "playbook for starting a new project from an idea, putting it on an existing repo, and running, watching or "
        "stopping it.")}}))
    if seen:
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.touch()
