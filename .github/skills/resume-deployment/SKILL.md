---
name: resume-deployment
description: A deployment stopped halfway (azd up failed, was cancelled or timed out) - runs a full health check plus your pasted error, finds where it stopped and whether it is safe to resume, and offers to do the next step. Use for "can I resume my deployment?", "azd up stopped in the middle", or "continue where it left off".
argument-hint: optional resource group name or the error azd showed
---

1. **Ask for the error first** (one short question), unless the user already gave it: "Did `azd up` show an error? Paste
   it here (or say 'no error')." Wait for the answer.
2. Run the `resume_plan` tool of the accelerator-troubleshooter MCP server with `error` set to the pasted text (leave it
   out if there is none) and `resource_group` if the user named one. It runs a **full health check** itself and decides
   from azd's own provision deployment, so never say the infrastructure is deployed unless the tool says
   "azd provision ... Succeeded".

Answer in **exactly this order**: **Verdict** (the tool's label, unchanged: NOTHING TO RESUME, NOTHING HALTED, WAIT,
FIX FIRST, SAFE TO RESUME, SAFE TO RESUME THE LAST STEP), **Where it stopped** (one bullet per step with ✅ / ❌ / ⏳ and the
tool's detail), **Why** (the reasons, plainly, including what the pasted error means), **Also found** (other health-check
findings, if any), **Next step**, then "Ask next", then **Sources**.

**Next step:**
- FIX FIRST with an allow-listed fix (for example the policy tag or starting a stopped app): show its commands and risk,
  ask a plain yes/no, and call `apply_fix` with `user_approved=true` only after yes; then call `resume_plan` again.
- SAFE TO RESUME / SAFE TO RESUME THE LAST STEP: give the exact command in a code block and recommend running it in the
  user's terminal as `python -m accel_troubleshooter run -- <command>` (it diagnoses automatically if it fails), or
  `python -m accel_troubleshooter resume` there, which offers to run it after they type yes. You do not run `azd`
  yourself: it is long-running and interactive.
- WAIT: say what is still running and to check again later.
If the report says the post-provision step cannot be verified, say so and ask for its error; never claim it finished.

**Sources** (last, one line each, each starting with a bold origin tag): **[Repo docs]** 📘, **[Repo code]** 🧩 (for
example `azure.yaml` hooks), **[Live Azure]** 🔬 (the check ids: `deploy.operations`, `resources.state`, `webapp.state`,
`azure.fabric`), **[External]** 🌐 only if used.
