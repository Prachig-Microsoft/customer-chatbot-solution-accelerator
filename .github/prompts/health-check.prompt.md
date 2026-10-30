---
description: "Full read-only health check of my deployed accelerator (current azd environment)"
name: "health-check"
argument-hint: "optional: a resource group name to check instead of the current azd environment"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Run a full health check of my deployed accelerator with the `health_check` tool. If I gave a resource group below, pass it
as `resource_group`; otherwise use the current azd environment.

Resource group (optional): ${input:resourceGroup:leave empty for the current azd environment}

Then answer in this shape, using the tool's status and confidence labels exactly as written (do not upgrade or downgrade them):
- **Verdict:** healthy / not healthy, and the one most important finding.
- **Confirmed findings** with the evidence the tool gave (check id and resource names).
- **What passed** in one line.
- **What is still unverified** (for example a real signed-in chat message).
- **Next step:** the smallest safe action and its category (🟢 read-only, 🟡 needs approval, 📘 manual, 🔴 restricted).
