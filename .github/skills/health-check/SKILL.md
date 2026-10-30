---
name: health-check
description: After deploying - full read-only health check of the deployed accelerator (current azd environment, or a resource group you name). Use for "is my deployment healthy?", "health check", or "check resource group rg-xyz".
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Run the `health_check` tool of the accelerator-troubleshooter MCP server. If the user gave a resource group (the text
after the command), pass it as `resource_group` (it works for any resource group, not just the current azd environment;
if that group is in another subscription, tell the user to run `az account set --subscription <id>` first). Otherwise use
the current azd environment.

Then answer in **exactly this format and order**, using the tool's status and confidence labels exactly as written (never
upgrade or downgrade them). Keep the body clean: **no file paths, check ids or URLs inside the sections**; all sources go
in the Sources block at the very bottom.

**Verdict:** healthy / not healthy, one or two sentences with the single most important finding.

**Confirmed findings** (RG `<resource group>`, region `<region>`)
- one bullet per finding, with ⚠️ / ❌ / ℹ️ and plain-language detail (what, which resource, when). If the report gives no
  detail for a warning, you may read it with the same read-only query and say so.
- anything that is your own interpretation rather than a tool label must be marked "(my interpretation)".

**What passed**
- one short bullet per area (login and providers, resource group, quota and models, model deployments, apps and health
  probes, RBAC, config match).

**Still unverified**
- what the checks cannot prove (for example a real signed-in chat message), and any finding the report did not detail.

**Next step:** one line with the smallest safe action and its category (🟢 read-only, 🟡 needs approval, 📘 manual,
🔴 restricted).

Ask next: two or three follow-ups.

**Sources** (always last, one line each, so the answer above stays easy to read)
- 🔬 live checks used: the check ids and the Azure CLI command each ran (for example `network.public_access`
  - `az cosmosdb show`).
- 📘 accelerator docs / IaC cited: `path:line`.
- 🌐 external: URLs, only if you used any (mark "not from the accelerator docs").

If the tool proposes an approval-required fix, show its commands and risk, ask a plain yes/no question, and call
`apply_fix` only after the user answers yes. When the fix is the temporary `SecurityControl` tag, also give the permanent
fix (`add-tag-to-template`) as a suggestion.
