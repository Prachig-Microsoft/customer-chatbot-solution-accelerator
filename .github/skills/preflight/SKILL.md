---
name: preflight
description: Before deploying (azd up) - check model quota and App Service plan quota in your Azure subscription, the recommended AI region and primary location, and any blocker, with a clear GO / NO-GO. Use for "is it safe to deploy?", "check quota", or before the first azd up.
argument-hint: optional AI region to evaluate, e.g. swedencentral
---

Run the `preflight` tool of the accelerator-troubleshooter MCP server (pass `region` only if the user gave one). If the
user asked specifically about quota or capacity, also call `quota_check`.

Then answer in **exactly this shape** (it matches what the team agreed on; use the tool's labels and numbers as written).

First line, as a heading: `## ✅ GO`, `## 🛑 NO-GO` or `## ❔ NOT CHECKED` (never GO when the tool says NOT CHECKED).
When the report says **"GO (models only; App Service quota not checked)"**, use exactly that heading; never shorten it to
a plain GO. Then these bullets, in this order:

- **Recommended AI region (models):** `<region>` — one clause on why (your azd choice, or best free model quota) and
  that the live checks confirm quota and availability of the models, versions and deployment types. When the report
  says model quota is **not applicable** (the accelerator deploys no Azure OpenAI models), say that instead, give the
  verdict from the other checks (login, providers, configuration), and in the quota bullet write one sentence rather
  than a table. Never invent a model table.
- **Primary location (apps, data):** `<location>` from the report's "Primary location" line and why. This is a
  **separate** setting from the AI region (the report names the variable, usually `AZURE_LOCATION`): the App Service
  plans and other app and data resources are created here. Never present the AI region as the place to deploy
  everything. If the report says App Service quota was not checked, say so plainly.
- **Live quota (K TPM):** followed by a table with the columns **Model | Needed | Free | Scope**, one row per model.
  Scope is "Subscription-wide shared pool" for global pools, or "Per region (`<region>`)" for regional ones.
- **Compute quota (App Service):** followed by a table **Location | SKU | Needed | Limit | Free | Result** from the
  report's "Compute quota" section (the plan SKU and instances it found in the Bicep, for the deployment flavor it
  names). Say where the SKU comes from (file and line). Leave the bullet out only when the report says there is no App
  Service plan in the template.
- **Needed values:** where each "needed" number came from, as links with file and line (the Bicep default, the
  parameters file, or the azd environment value), plus one short sentence that tells whether switching regions would
  change the quota (it does not for subscription-wide pools).
- **Tools and versions:** always include this bullet when the report has a "Tools (as the docs ask)" table: each tool
  with the installed version and the minimum the documents ask for, ✅ or ⚠️ (for example "⚠️ Python 3.11.9, docs ask
  >= 3.12"), and name any missing tool or Azure CLI extension and which document asks for it (a tool the docs mark
  optional, such as Docker for the Dev Container route, is not a blocker). Mention "also installed" tools only briefly.
  Do not fold tool problems into Warnings; they belong here.
- **Blockers:** none, or the blockers with the smallest fix. For an App Service quota blocker give the options in this
  order, safest first: (1) 🟡 choose a primary location the report shows has quota (`azd env set AZURE_LOCATION
  <location>`); (2) 🔴 request the quota (https://aka.ms/antquotahelp; needs someone with quota rights to approve);
  (3) 📘 change the plan SKU in the Bicep at the file and line the report names (a template change). **Warnings:** one
  sentence on what the tool warned about (for example missing azd values, documents that disagree with the template,
  uncommitted changes).
- **Security reminder:** only when the tool shows the `SecurityControl` policy tag, one sentence on what it is.
- **Next deployment command, only if you intend to provision/update:** then the exact command in a code block
  (`azd up`). Say it was not run; never run it yourself.

Mark anything that is your own interpretation "(my interpretation)". Then one line "Ask next: …" with two or three
follow-ups, and last the **Sources**, one line each, starting with a bold origin tag in this order (leave out a tag that
has nothing to show):
- **[Repo docs]** 📘 the accelerator's own documentation: `path:line`.
- **[Repo code]** 🧩 its Bicep, scripts or parameter files: `path:line`.
- **[Live Azure]** 🔬 read-only checks run just now: the check ids (for example `quota.models`, `quota.compute`) and the
  command.
- **[External]** 🌐 URLs, only if you used any (mark "not from the accelerator docs, please verify").
