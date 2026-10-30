---
name: preflight
description: Before deploying (azd up) - check model quota in your Azure subscription, the recommended region, and any blocker, with a clear GO / NO-GO. Use for "is it safe to deploy?", "check quota", or before the first azd up.
argument-hint: optional AI region to evaluate, e.g. swedencentral
---

Run the `preflight` tool of the accelerator-troubleshooter MCP server (pass `region` only if the user gave one). If the
user asked specifically about quota or capacity, also call `quota_check`.

Answer with: the **GO / NO-GO verdict first**, the recommended AI region and why, then **quota** (what the deployment
needs versus what is free, read live from the user's Azure subscription; say which quota is one shared pool for the whole
subscription and which is per region), then **blockers** (or "none"), then the exact next command.

State where the "needed" numbers came from (the tool names the file and line, or the azd environment value, or the
parameters file) so the user can see that edits to the parameter file or Bicep were picked up. Keep it under ten
bullets, use the tool's labels as written, and include the reminder about the `SecurityControl` tag when the tool shows
it.
