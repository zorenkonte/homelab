---
name: statusline-preferences
description: "User's Claude Code status line lives at ~/.claude/statusline-command.sh; jq is absent so JSON is parsed with python3; prefers 24h times, 5h usage always shown with reset, weekly without reset"
metadata:
  type: feedback
---

The status line script (set up 2026-10-06) is the user's own colored one-liner design, kept verbatim except the jq pass was ported to python3 because jq is not installed on the Pi and installing packages is off-limits (see [[runner-bot-security-constraints]]). Preferences expressed: 24-hour clock, show the 5-hour usage and its reset time at every level (LIMIT_AT=0), show weekly usage without a reset time. No per-model (Fable) usage exists in the status line JSON; only five_hour and seven_day.

**Why:** the user adjusts the status line often and wants changes applied to their script, not replaced with a new design.
**How to apply:** edit `/root/.claude/statusline-command.sh` in place, test with piped sample JSON, never introduce a jq dependency.
