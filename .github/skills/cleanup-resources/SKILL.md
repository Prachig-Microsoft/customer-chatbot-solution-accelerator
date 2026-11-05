---
name: cleanup-resources
description: Plan the cleanup of a deployment (azd down) - what would be deleted, what blocks it, what the accelerator's own docs and hooks say, and the exact commands. Can delete one resource group after you type its exact name and confirm a second time. Use for "clean up my deployment", "azd down", "delete the resource group", or "stop the billing".
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Run the `cleanup_plan` tool of the accelerator-troubleshooter MCP server (pass `resource_group` if the user gave one).

Answer in **exactly this order**: **What would be deleted** (count, main resource types, and anything that needs a purge
because Azure keeps it after deletion), **Watch out** (locks, resources not created by this azd environment, a Fabric
capacity that keeps billing while Active), **What the accelerator says** (its `predown` hook, teardown script and the
document lines the tool cites), **Steps** (the tool's commands, in order, in code blocks), "Ask next", then **Sources**.

In "Ask next", offer: "I can delete this resource group for you: you will be asked to type its exact name, then to
confirm once more, in confirmation forms."

If the user wants the agent to delete it, call `delete_resource_group` once with the group name. The tool re-checks the
group and then asks the user itself, in two VS Code confirmation forms you cannot see or fill, to type the exact name and
then `delete <name>`. Do not ask for those in chat and never pass them yourself. One resource group only. You cannot
run commands: never offer to run `azd down`, a purge, a lock removal or a pause/resume. If the tool says the client
cannot show confirmation forms, give the terminal command it prints. Mention that a plain group delete skips the
accelerator's `predown` hook when the tool says so, and that `azd down` is the route that runs it.

If a Fabric capacity exists, also mention pausing it as the cheaper alternative when they only want to stop billing but
keep the data.

**Sources** (last, one line each, each starting with a bold origin tag): **[Repo docs]** 📘, **[Repo code]** 🧩 (for
example `azure.yaml` and the removal script), **[Live Azure]** 🔬 (`cleanup.inventory` and its commands), **[External]** 🌐
only if used.
