---
name: preflight
description: Before deploying (azd up) - check model quota in your Azure subscription, the recommended region, and any blocker, with a clear GO / NO-GO. Use for "is it safe to deploy?", "check quota", or before the first azd up.
argument-hint: optional AI region to evaluate, e.g. swedencentral
---

Run the `preflight` tool of the accelerator-troubleshooter MCP server (pass `region` only if the user gave one). If the
user asked specifically about quota or capacity, also call `quota_check`.

Then answer in **exactly this shape** (it matches what the team agreed on; use the tool's labels and numbers as written).

First line, as a heading: `## ✅ GO`, `## 🛑 NO-GO` or `## ❔ NOT CHECKED` (never GO when the tool says NOT CHECKED). Then
these bullets, in this order:

- **Recommended AI region:** `<region>` — one clause on why (your azd choice, or best free quota) and that the live
  checks confirm quota and availability of the models, versions and deployment types. When the report says model quota
  is **not applicable** (the accelerator deploys no Azure OpenAI models), say that instead, give the verdict from the
  other checks (login, providers, configuration), and in the quota bullet write one sentence rather than a table.
  Never invent a model table.
- **Live quota (K TPM):** followed by a table with the columns **Model | Needed | Free | Scope**, one row per model.
  Scope is "Subscription-wide shared pool" for global pools, or "Per region (`<region>`)" for regional ones.
- **Needed values:** where each "needed" number came from, as links with file and line (the Bicep default, the
  parameters file, or the azd environment value), plus one short sentence that tells whether switching regions would
  change the quota (it does not for subscription-wide pools).
- **Tools and versions:** always include this bullet when the report has a "Tools (as the docs ask)" table: each tool
  with the installed version and the minimum the documents ask for, ✅ or ⚠️ (for example "⚠️ Python 3.11.9, docs ask
  >= 3.12"), and name any missing tool or Azure CLI extension and which document asks for it (a tool the docs mark
  optional, such as Docker for the Dev Container route, is not a blocker). Mention "also installed" tools only briefly.
  Do not fold tool problems into Warnings; they belong here.
- **Blockers:** none, or the blockers with the smallest fix. **Warnings:** one sentence on what the tool warned about
  (for example missing azd values, documents that disagree with the template, uncommitted changes).
- **Security reminder:** only when the tool shows the `SecurityControl` policy tag, one sentence on what it is.
- **Next deployment command, only if you intend to provision/update:** then the exact command in a code block
  (`azd up`). Say it was not run; never run it yourself.

Mark anything that is your own interpretation "(my interpretation)". Then one line "Ask next: …" with two or three
follow-ups, and last the **Sources**, one line each, starting with a bold origin tag in this order (leave out a tag that
has nothing to show):
- **[Repo docs]** 📘 the accelerator's own documentation: `path:line`.
- **[Repo code]** 🧩 its Bicep, scripts or parameter files: `path:line`.
- **[Live Azure]** 🔬 read-only checks run just now: the check ids (for example `quota.models`) and the command.
- **[External]** 🌐 URLs, only if you used any (mark "not from the accelerator docs, please verify").
