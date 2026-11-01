#!/usr/bin/env python3
"""UserPromptSubmit hook: name the session "Mirror-Review YYYY-MM-DD".

Fires on every prompt; acts only when the prompt invokes /mirror-review.
"""
import datetime
import json
import sys

event = json.load(sys.stdin)
command = event.get("prompt", "").split(maxsplit=1)

if command and command[0] == "/mirror-review":
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "sessionTitle": f"Mirror-Review {datetime.date.today().isoformat()}",
        }
    }))
