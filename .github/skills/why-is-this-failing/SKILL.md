---
name: why-is-this-failing
description: Diagnose an error (azd up output, post-provision script error, app error). Looks in the accelerator's own docs first, then wider sources, and always says where each answer came from.
argument-hint: paste the error text, or describe what is going wrong
---

Diagnose the problem with the `diagnose` tool of the accelerator-troubleshooter MCP server. Pass the user's text (what
they typed after the command, or ask them to paste it) as `issue` exactly as written; do not summarise it first.

**Order of sources (never skip ahead):**
1. **The accelerator's own docs and scripts.** Use the report's cited documentation and, for a specific error code or
   phrase, `search_docs`.
2. **Live read-only Azure checks** the tool ran (they are evidence, not advice).
3. **Only if 1 and 2 give no solution:** search wider (official Microsoft Learn and Azure docs, then the accelerator's
   GitHub issues at the upstream repository the tool names). Say plainly "the accelerator docs have no entry for this".

**Label the source of every statement**, one of:
- 📘 accelerator docs: `path:line`
- 🔬 live check: check id (for example `network.public_access`)
- 🌐 external: the URL, marked "not from the accelerator docs, please verify"

Then answer with: the **most likely cause** and its status/confidence exactly as the tool labelled them, the evidence,
what is still missing, and the smallest safe fix with its category (🟢 / 🟡 / 📘 / 🔴). Never claim anything was fixed
or run unless the tool output shows it. For a 🟡 fix, ask yes/no and call `apply_fix` only after the user says yes. If
you have no web access, say so instead of guessing.
