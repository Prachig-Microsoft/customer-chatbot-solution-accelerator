---
description: "Is it safe to run azd up? GO / NO-GO with quota, region and model availability"
name: "preflight"
argument-hint: "optional: an AI region to evaluate"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Run the `preflight` tool (pass a `region` only if I gave one: ${input:region:leave empty}).

Answer with: the **GO / NO-GO verdict first**, the recommended AI region and why, free quota against what the deployment
needs, anything blocking, and the exact next command. Keep it under ten bullets and use the tool's labels as written.
