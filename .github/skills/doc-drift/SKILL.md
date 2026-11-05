---
name: doc-drift
description: Is the documentation up to date? Compares the accelerator's docs and scripts with its Bicep and parameter files and lists every mismatch with file, line and fix. Offline, nothing is changed.
argument-hint: optional accelerator id, e.g. content-processing
---

Call the `doc_drift` tool of the accelerator-troubleshooter MCP server (pass `accelerator` only if the user named one).

Answer in short bullets, most important first (high, then medium, then low): for each finding give the file and line, what
disagrees (for example "docs say capacity 10, the template default is 50"), and the fix the tool names. Say how many
findings and in how many files. If there is none, say the docs match the template.

Offer: "Want me to fix the top items in the docs?" and edit documentation only if the user says yes. Say that the full
list of document gaps (these findings plus tool versions that documents disagree on, missing documents and broken links)
was saved to the local, git-ignored `.runs/document-gaps.md` under the troubleshooter folder, and that
`drift --format full --out drift.md` writes a PR-ready checklist. End with "Sources", each line starting with an origin
tag: **[Repo docs]** 📘 or **[Repo code]** 🧩 with the files and lines the tool cited.
