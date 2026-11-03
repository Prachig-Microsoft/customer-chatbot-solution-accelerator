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
  probes, app error logs, RBAC, config match).

**Still unverified**
- what the checks cannot prove (for example a real signed-in chat message), and any finding the report did not detail.

**Next step:** one line with the smallest safe action and its category (🟢 read-only, 🟡 needs approval, 📘 manual,
🔴 restricted). When everything looks healthy but the report says a real chat reply is "Not proven yet", the next step
is the 🟡 real chat test: ask "Send one real test chat message with your sign-in (one model call, the test session is
deleted afterwards)? yes / no" and call `chat_test` with `user_approved=true` only after yes.

Ask next: two or three follow-ups.

**Sources** (always last, one line each, so the answer above stays easy to read). Start every line with a bold origin tag
so it is obvious where it came from, and group the lines in this order (leave out a tag that has nothing to show):
- **[Repo docs]** 📘 the accelerator's own documentation: `path:line`.
- **[Repo code]** 🧩 its Bicep, scripts or app code: `path:line` (for example `infra/main.bicep:49`).
- **[Live Azure]** 🔬 read-only checks run just now: the check id and the command (for example `network.public_access`
  - `az cosmosdb show`; `appinsights.errors` for what the apps logged; `chat.e2e` for a real chat message).
- **[External]** 🌐 URLs, only if you used any (mark "not from the accelerator docs, please verify").

If the tool proposes an approval-required fix, show its commands and risk, ask a plain yes/no question, and call
`apply_fix` only after the user answers yes. When the fix is the temporary `SecurityControl` tag, also give the permanent
fix (`add-tag-to-template`) as a suggestion.
