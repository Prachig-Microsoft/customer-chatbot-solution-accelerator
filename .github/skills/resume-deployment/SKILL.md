---
name: resume-deployment
description: A deployment stopped halfway (azd up failed, was cancelled or timed out) - find out where it stopped and whether it is safe to resume, and with which command. Read-only. Use for "can I resume my deployment?", "azd up stopped in the middle", or "continue where it left off".
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Run the `resume_plan` tool of the accelerator-troubleshooter MCP server (pass `resource_group` if the user gave one).

Answer in **exactly this order**: **Verdict** (the tool's label, unchanged: NOTHING TO RESUME, NOTHING HALTED, WAIT, FIX
FIRST, SAFE TO RESUME, SAFE TO RESUME THE LAST STEP), **Where it stopped** (one bullet per step with ✅ / ❌ / ⏳ and the
tool's detail), **Why** (the reasons, plainly), **Next step** (the exact command from the tool in a code block, with its
category: 🟡 needs approval or costs money, 📘 manual), then "Ask next", then **Sources**.

Rules: Azure cannot truly continue a half-finished deployment; `azd provision` re-applies the template and creates only
what is missing, so say "resume" means "re-run safely". Never run `azd`, `az ... cancel` or any deployment command
yourself: you only show it. When the verdict is FIX FIRST, show the fix the tool names (and run `apply_fix` only if it is
an allow-listed fix and the user says yes), then tell them to re-run the plan. If the report says the post-provision
step cannot be verified, say so and do not claim it finished.

**Sources** (last, one line each, each starting with a bold origin tag): **[Repo docs]** 📘, **[Repo code]** 🧩 (for
example `azure.yaml` hooks), **[Live Azure]** 🔬 (the check ids: `deploy.operations`, `resources.state`, `azure.fabric`),
**[External]** 🌐 only if used.
