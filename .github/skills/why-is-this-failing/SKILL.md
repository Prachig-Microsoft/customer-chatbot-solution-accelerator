---
name: why-is-this-failing
description: Diagnose a pasted error or symptom (azd up output, post-provision script error, app error) using the accelerator's own docs, IaC and live read-only Azure checks.
argument-hint: paste the error text, or describe what is going wrong
---

Diagnose the problem with the `diagnose` tool of the accelerator-troubleshooter MCP server. Pass the user's text (what
they typed after the command, or ask them to paste it) as `issue` exactly as written; do not summarise it first.

Then answer with: the **most likely cause** and its status/confidence exactly as the tool labelled them, the evidence
and the doc path with line number, what is still missing, and the smallest safe fix with its category (🟢 / 🟡 / 📘 / 🔴).
Never claim anything was fixed or run unless the tool output shows it. For a 🟡 fix, ask yes/no and call `apply_fix`
only after the user says yes.
