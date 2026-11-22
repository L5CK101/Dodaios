---
schema_version: "1"
name: plugin-snu_etl
description: List the user's SNU eTL assignments, due dates and whether each is submitted.
when_to_use: Use when the user mentions eTL, SNU, Canvas, homework, assignments, due dates or deadlines, or asks whether they submitted something.
category: knowledge
plugin_id: snu_etl
intent_verbs: [show, list, check, what]
intent_objects: [etl, canvas, assignment, assignments, homework, deadline, deadlines, due]
triggers:
  - type: voice
    pattern: "(etl|e-tl|canvas|assignment|homework|due date|deadline)"
requires_tools: [etl]
risk_policy:
  default_tier: monitor
---

Use the `etl` tool to answer from the user's real eTL data — read-only.

- "What's due?" → `upcoming` (default 14 days; pass `days` when the user names a range).
- "What haven't I submitted?" → `unsubmitted`. "eTL summary" → `summary`. "My courses" → `courses`.
- Report course, title, due time (Korean time, as given) and status. Lead with what is due soonest and not submitted.
- This tool never submits work or changes anything in eTL; say so if asked to submit.
- If the tool says eTL is not connected or the token was rejected, tell the user to (re)connect SNU eTL in the Plugins view. Never ask for the token in chat.
