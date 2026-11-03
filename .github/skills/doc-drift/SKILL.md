---
name: doc-drift
description: Is the documentation up to date? Compares the accelerator's docs and scripts with its Bicep and parameter files and lists every mismatch with file, line and fix. Offline, nothing is changed.
argument-hint: optional accelerator id, e.g. content-processing
---

Call the `doc_drift` tool of the accelerator-troubleshooter MCP server (pass `accelerator` only if the user named one).

Answer in short bullets, most important first (high, then medium, then low): for each finding give the file and line, what
disagrees (for example "docs say capacity 10, the template default is 50"), and the fix the tool names. Say how many
findings and in how many files. If there is none, say the docs match the template.

Offer: "Want me to fix the top items in the docs?" and edit documentation only if the user says yes. Mention that
`drift --format full --out drift.md` writes a PR-ready checklist. End with "Sources": the files and lines the tool cited.
