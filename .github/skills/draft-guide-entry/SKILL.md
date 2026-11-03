---
name: draft-guide-entry
description: Draft a new troubleshooting-guide row from an error you diagnosed or fixed - generalised (no names, ids or secrets), in the guide's own format, with the section and line. It only prints the draft.
argument-hint: the error text, and optionally what fixed it
---

Call the `draft_guide_entry` tool of the accelerator-troubleshooter MCP server. Pass the user's error text as `issue`
exactly as written, the resource group if they gave one, and what fixed it (if they said) as `note`.

Show the drafted row, which section it belongs to and after which line. If the tool says the guide already documents the
problem, say so and cite that row instead. If it found no cause and the user gave no fix, ask them what fixed it, then
call the tool again with it as `note`.

Remind them to read the row and remove anything specific to their tenant. **Only if the user asks you to add it**, insert
the row after the stated line of the guide with your editor and show the change. Never add it on your own.
End with "Sources", each line starting with an origin tag: **[Live Azure]** 🔬 the checks the draft was built from;
**[Repo docs]** 📘 the guide and line it goes next to.
