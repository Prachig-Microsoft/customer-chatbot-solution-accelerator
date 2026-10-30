---
description: "Check model quota and capacity, and find the best region"
name: "quota-check"
argument-hint: "optional: a region, e.g. swedencentral"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Run the `quota_check` tool (region only if I gave one: ${input:region:leave empty for all allowed regions}).

Answer with a short table or bullets: each model, what is needed, what is free, which quota is one shared pool for the whole
subscription and which is per region, and the recommended region or capacity. Note that for a shared pool, changing region
does not help.
