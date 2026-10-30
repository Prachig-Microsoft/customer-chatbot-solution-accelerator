---
description: "Check another resource group (not the current azd environment) for health problems"
name: "check-resource-group"
argument-hint: "resource group name, e.g. rg-mydeployment"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Run `health_check` for this resource group: ${input:resourceGroup:rg-name}

Pass it as `resource_group`. Do not use the current azd environment's values: the tool already ignores them for a different
resource group and discovers the apps, AI account and region from Azure.

If the tool says the group does not exist, say so and suggest the user check the subscription (`az account show`) and run
`az account set --subscription <id>` if the group lives in another one. Report the tool's status and confidence labels
exactly as written.
