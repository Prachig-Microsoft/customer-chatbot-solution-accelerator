---
name: health-check
description: After deploying - full read-only health check of the deployed accelerator (current azd environment, or a resource group you name). Use for "is my deployment healthy?", "health check", or "check resource group rg-xyz".
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Run the `health_check` tool of the accelerator-troubleshooter MCP server. If the user gave a resource group (the text
after the command), pass it as `resource_group` (it works for any resource group, not just the current azd environment;
if that group is in another subscription, tell the user to run `az account set --subscription <id>` first). Otherwise use
the current azd environment.

Then answer in this shape, using the tool's status and confidence labels exactly as written (never upgrade or downgrade
them):

- **Verdict:** healthy / not healthy, and the one most important finding.
- **Confirmed findings** with their evidence and source (check id and resource names).
- **What passed** in one line.
- **What is still unverified** (for example a real signed-in chat message).
- **Next step:** the smallest safe action and its category (🟢 read-only, 🟡 needs approval, 📘 manual, 🔴 restricted).

If the tool proposes an approval-required fix, show its commands and risk, ask a plain yes/no question, and call
`apply_fix` only after the user answers yes. When the fix is the temporary `SecurityControl` tag, also give the permanent
fix (`add-tag-to-template`) as a suggestion.
