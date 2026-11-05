---
name: cleanup-resources
description: Plan the cleanup of a deployment (azd down) - what would be deleted, what blocks it, what the accelerator's own docs and hooks say, and the exact commands. Read-only: the agent never deletes anything. Use for "clean up my deployment", "azd down", "delete the resource group", or "stop the billing".
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Run the `cleanup_plan` tool of the accelerator-troubleshooter MCP server (pass `resource_group` if the user gave one).

Answer in **exactly this order**: **What would be deleted** (count, main resource types, and anything that needs a purge
because Azure keeps it after deletion), **Watch out** (locks, resources not created by this azd environment, a Fabric
capacity that keeps billing while Active), **What the accelerator says** (its `predown` hook, teardown script and the
document lines the tool cites), **Steps** (the tool's commands, in order, in code blocks), "Ask next", then **Sources**.

Rules: this is destructive and cannot be undone, so you only **show** commands. Never run `azd down`, `az group delete`,
a purge, a lock removal or a pause/resume yourself, even if the user says "go ahead": tell them to run it in their own
terminal after checking the resource group and subscription. If the tool says resource locks block deletion, say so
first. If a Fabric capacity exists, also mention pausing it as the cheaper alternative when they only want to stop billing
but keep the data.

**Sources** (last, one line each, each starting with a bold origin tag): **[Repo docs]** 📘, **[Repo code]** 🧩 (for
example `azure.yaml` and the removal script), **[Live Azure]** 🔬 (`cleanup.inventory` and its commands), **[External]** 🌐
only if used.
