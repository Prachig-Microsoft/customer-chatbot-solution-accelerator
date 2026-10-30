---
name: preflight
description: Pre-deployment check before azd up - GO / NO-GO with quota, recommended region and model availability. Use when the user asks "is it safe to deploy?" or wants a quota check before deploying.
argument-hint: optional AI region to evaluate, e.g. swedencentral
---

Run the `preflight` tool of the accelerator-troubleshooter MCP server (pass `region` only if the user gave one).

Answer with: the **GO / NO-GO verdict first**, the recommended AI region and why, free quota against what the
deployment needs, anything blocking, and the exact next command. Keep it under ten bullets and use the tool's labels as
written.
