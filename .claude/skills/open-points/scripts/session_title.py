#!/usr/bin/env python3
"""UserPromptSubmit hook: name the session after the open point it works on.

Fires on every prompt; acts only when the prompt invokes /open-points.
  /open-points              -> "Open Points YYYY-MM-DD"
  /open-points <item>       -> "Open Point: <item> YYYY-MM-DD"
  /open-points add <text>   -> "Open Point add: <text> YYYY-MM-DD"
  /open-points done <item>  -> "Open Point done: <item> YYYY-MM-DD"
"""
import datetime
import json
import sys

MAX_ITEM = 50

event = json.load(sys.stdin)
parts = event.get("prompt", "").split(maxsplit=1)

if parts and parts[0] == "/open-points":
    today = datetime.date.today().isoformat()
    arg = " ".join(parts[1].split()) if len(parts) > 1 else ""
    mode, _, rest = arg.partition(" ")
    if not arg:
        title = f"Open Points {today}"
    elif mode in ("add", "done") and rest:
        title = f"Open Point {mode}: {rest[:MAX_ITEM]} {today}"
    else:
        title = f"Open Point: {arg[:MAX_ITEM]} {today}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "sessionTitle": title,
        }
    }))
