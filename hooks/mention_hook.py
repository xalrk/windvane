#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: when the user mentions windvane or autopilot, tell the session what that means
and to load the windvane skill, the way a keyword like "ultracode" switches a mode on. Installed by
`windvane install`. Silent (no output) for every other prompt."""
import json
import re
import sys

try:
    prompt = json.load(sys.stdin).get("prompt", "")
except ValueError:
    sys.exit(0)
if re.search(r"\bwind-?vane|\bauto-?pilot\b", prompt, re.I):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        "The user mentioned windvane / autopilot: their own Claude Code autopilot (~/Documents/windvane, command "
        "`windvane`), which builds a project ticket by ticket in fresh headless sessions with a critic, a reviewer, "
        "phone questions and parallel slots. Load the `windvane` skill (Skill tool) before acting: it is the "
        "playbook for starting a new project from an idea, putting it on an existing repo, and running, watching or "
        "stopping it.")}}))
