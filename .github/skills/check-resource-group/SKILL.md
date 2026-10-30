---
name: check-resource-group
description: Check a specific Azure resource group (not necessarily the current azd environment) for health problems. Use when the user names a resource group such as rg-xyz.
argument-hint: resource group name, e.g. rg-mydeployment
---

Run `health_check` of the accelerator-troubleshooter MCP server for the resource group the user gave (the text after the
command). If they did not give one, ask for it.

Pass it as `resource_group`. Do not use the current azd environment's values: the tool already ignores them for a
different resource group and discovers the apps, AI account and region from Azure.

If the tool says the group does not exist, say so and suggest checking the subscription (`az account show`) and running
`az account set --subscription <id>` if the group lives in another one. Report the tool's status and confidence labels
exactly as written. Offer an approval-required fix only through the yes/no flow and the `apply_fix` tool.
