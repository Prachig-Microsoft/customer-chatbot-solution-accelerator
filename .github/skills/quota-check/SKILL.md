---
name: quota-check
description: Check Azure OpenAI / Foundry model quota against what the deployment needs and find the best region.
argument-hint: optional region, e.g. swedencentral
---

Run the `quota_check` tool of the accelerator-troubleshooter MCP server (pass `region` only if the user gave one).

Answer with a short table or bullets: each model, what is needed, what is free, which quota is one shared pool for the
whole subscription and which is per region, and the recommended region or capacity. For a shared pool, changing region
does not help; say so.
