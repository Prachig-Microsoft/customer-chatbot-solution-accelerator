# Accelerator Troubleshooting Agent

An accelerator-aware troubleshooting engineer for Azure AI solution accelerators. It takes a problem described in
natural language, identifies the affected accelerator, reads that accelerator's **own** troubleshooting/deployment
documentation, IaC and scripts, collects **read-only** evidence from Azure, ranks root causes with explicit evidence,
recommends the smallest safe fix, executes approved fixes, and verifies the result.

It runs as a CLI, as an MCP server for GitHub Copilot agent mode, and through a Copilot custom agent
([`.github/agents/accelerator-troubleshooter.agent.md`](../.github/agents/accelerator-troubleshooter.agent.md)).
The core uses only the Python standard library. No hosted model, vector database, or paid service is required.

## Quick start

```powershell
cd troubleshooter

# 1. Before deploying: GO / NO-GO + quota (about 30 lines)
python -m accel_troubleshooter preflight

# 2. Deploy through the wrapper: output is saved; if it fails you get a diagnosis automatically
python -m accel_troubleshooter run -- azd up
python -m accel_troubleshooter run -- infra\scripts\post-provision\build_push_images.ps1
python -m accel_troubleshooter run -- infra\scripts\post-provision\postprovision_data_agents.ps1

# 3. Any time: describe the problem, or paste the error. All of these work:
python -m accel_troubleshooter diagnose "chat returns 500 errors"
python -m accel_troubleshooter diagnose                      # prompts; paste a multi-line error, then Enter twice
python -m accel_troubleshooter diagnose --from-clipboard     # use what you just copied
python -m accel_troubleshooter diagnose --log deploy.log     # or a saved log file
python -m accel_troubleshooter diagnose --last               # output of the last `run`
Get-Content deploy.log | python -m accel_troubleshooter diagnose -      # stdin

python -m accel_troubleshooter health                        # full read-only health check of the current azd env
python -m accel_troubleshooter watch                         # follow `azd up` running in ANOTHER terminal; diagnoses at the first failure
python -m accel_troubleshooter about                         # what is this accelerator (description from its README)
python -m accel_troubleshooter chat-test                     # health + ONE real signed-in chat message (asks first, deletes the test session)
python -m accel_troubleshooter drift                         # where docs/scripts disagree with the Bicep + parameter files (offline)
python -m accel_troubleshooter draft-guide "<error>" --note "what fixed it"   # draft a new troubleshooting-guide row (prints only)
python -m accel_troubleshooter health --window PT15M         # app error logs from the last 15 minutes only (default 1 hour)
python -m accel_troubleshooter quota                         # quota table for every allowed AI region
python -m accel_troubleshooter search "InsufficientQuota"    # cited doc retrieval
python -m accel_troubleshooter eval                          # regression / evaluation suite (no Azure needed)
./demo.ps1                                                   # reproducible demo (add -Live for live Azure parts)
```

### Resource group: automatic, then asks

Every command that needs a resource group finds it the same way, and prints where it came from on the `Target:` line:

1. `--resource-group` if you pass it
2. `AZURE_RESOURCE_GROUP` in the current azd environment (`.azure/<env>/.env`, env chosen by `.azure/config.json`)
3. an `rg-...` name in the error text you pasted
4. `rg-<azd env name>` if it exists in Azure (azd's naming convention)
5. a resource group tagged `azd-env-name=<env>`
6. otherwise **it asks you** ("Resource group name (Enter to skip)"). `--no-prompt`, CI and the MCP server never ask;
   they list the resource group under "Still needed" instead.

**A resource group that is not your current azd environment** (a colleague's, an older one, another region):

```powershell
python -m accel_troubleshooter health --resource-group rg-other
python -m accel_troubleshooter health --resource-group rg-other --region swedencentral   # only if auto-detect is wrong
az account set --subscription <id>; python -m accel_troubleshooter health --resource-group rg-other   # other subscription
```

When the group differs from the azd environment, the environment's app names, URLs and capacities are ignored (they
belong to another deployment) and everything is discovered from Azure; the AI region comes from the AI account found in
that group.

### Output

The default output is a compact **bullet** summary (verdict or cause, one bullet per check, quota, top findings with their
evidence and doc references, next commands, what is still missing). It is coloured on a real terminal (green = OK /
GO, red = failure / NO-GO, yellow = warning or unconfirmed, cyan = commands, dim = detail). Colour is off when output is
piped or redirected, with `--plain` (also ASCII markers instead of emoji) or `--no-color`, or when `NO_COLOR` is set;
`--color` forces it on. `--format full` prints the complete Markdown report, `--out report.md` saves the complete
report while still printing the summary, and `--format json` is for tooling.

### `run -- <command>`

Runs `azd`, `az`, `pwsh`/`bash`, or a `.ps1`/`.sh` script inside the accelerator repo (nothing else), in the repo root.
Output is saved to `.runs/last-run.log`.

- On **failure** it diagnoses from the captured error text plus live Azure state, and saves the full report to
  `.runs/last-report.md`.
- On **success** it runs a quick health check and prints the next script to run, taken from the accelerator profile.
  Findings about placeholder images or missing agents are expected until those scripts have run.
- `azd up`/`provision` need a real terminal for their prompts, so their output is **not** captured: the diagnosis then
  reads the failure from Azure (deployment operations carry the real error codes) and offers to take pasted error text.
  Use `--capture` to force capture, `--passthrough` to never capture, `--no-check` to skip the post-run check.

Useful options: `--resource-group`, `--region`, `--env <azd env>`, `--env-file <file>`, `--accelerator <id>`,
`--offline` (docs + repo only), `--replay <fixture.json>`, `--record <fixture.json>` (sanitized + anonymized),
`--capacity gptDeploymentCapacity=80` (what-if), `--full` (run every check), `--only a,b`, `--no-prompt`.
Exit codes: `0` no supported root cause, `1` findings, `2` clarification needed, `3` error.

## Supported accelerators

| Profile | Repository (discovered locally) | Coverage |
|---|---|---|
| `customer-chatbot` | this repo (`microsoft/customer-chatbot-solution-accelerator`) | **Full**: docs, IaC drift, quota/models, ARM deployments, resources, App Service images/settings/health, managed-identity and deployer RBAC, network access, local-vs-cloud config |
| `content-processing` | `../content-processing-solution-accelerator` | Docs, identification, quota/models (gpt-5.1), ARM deployments, resources. App-level (Container Apps) checks are not implemented yet. |
| `chat-with-your-data` | `../CWYD/chat-with-your-data-solution-accelerator` | Docs, identification, quota/models (incl. `OpenAI.Standard` embeddings), ARM deployments, resources. App-level checks are not implemented yet. |

Profiles live in [`accel_troubleshooter/profiles/`](accel_troubleshooter/profiles). Each one lists the accelerator's
fingerprint, docs per category, IaC parameter names, azd env keys, apps, expected RBAC (with Bicep source lines), and
post-provision scripts. Accelerators really do differ (e.g. CWYD uses `AZURE_AI_SERVICE_LOCATION` /
`AZURE_GPT_MODEL_CAPACITY`, the chatbot uses `AZURE_ENV_*`), so env-var mappings are read from each repo's
`main.parameters.json` instead of being assumed. Pass `--repo <id>=<path>` if a repository lives elsewhere.

## Architecture

```
issue text ──► classify.py ──► identify accelerator (workspace fingerprint, aliases, resource names; asks if ambiguous)
                 │                extract codes/regions/models/resources/timestamps; stage + category (taxonomy.json)
                 ▼
            engine.py (single orchestrator)
                 ├─ knowledge.py   BM25 index of docs (troubleshooting table rows + sections) and scripts, with path,
                 │                 line range, repo, branch, commit; freshness + coverage
                 ├─ diagnostics/   registry of read-only checks selected by stage (repo_checks, azure_checks, app_checks)
                 │                 via azcli.py (allow-list, no shell metacharacters, timeouts, redaction, replay/record)
                 ├─ rca.py         rules → hypotheses: confirmed / likely / possible + explicit confidence rationale,
                 │                 contradicting + missing evidence, doc/source refs, remediation ids
                 ├─ actions.py     remediation catalog: read_only / approval_required / restricted + gated executor
                 └─ report.py      Markdown/JSON incident report;  telemetry.py JSONL run log + stats
```

### Evidence and confidence model

Confidence is qualitative and every value comes with stated reasons. There are no numeric scores.

- **high**: the causal condition was observed directly (live check or repo) and matches the symptom, or the exact error
  code was observed in Azure.
- **medium**: the condition was observed but its link to the symptom is indirect, or a documented error code was
  reported by the user but has not been observed live.
- **low**: only textual similarity.

Hypotheses also carry `contradicting` evidence (e.g. "quota is sufficient *now* in that region") and
`missing_evidence`. When no evidence supports a cause, the agent says so and lists what to collect rather than guessing.

### Safety model

| Category | Examples | Behaviour |
|---|---|---|
| 🟢 read-only | all diagnostics, `quota_check_params.sh` | diagnostics run automatically; listed scripts are for you to run |
| 🟡 approval required | `azd env set` capacity/region, `az provider register`, restart/start web app, rerun post-provision scripts, re-create a template-declared role assignment, **add the policy's exemption tag and re-enable Cosmos public access** (`apply-exemption-tag`) | only tool-generated commands on the executor allow-list (`EXECUTABLE_ACTIONS`) run, and only after you answer **yes** (terminal: "Apply this fix now? Type 'yes'", or `fix <id> --approve`; chat: the agent asks yes/no, then calls the gated `apply_fix` tool). Verification checks and a full health check re-run afterwards. Long-running/cost-incurring steps (`azd provision`, new env, log streaming) are shown but you run them yourself. |
| 📘 documented steps | resolution steps copied from the accelerator's troubleshooting guide | displayed with their doc reference; never executed (some are destructive, e.g. `azd down --purge`) |
| 🔴 restricted | delete RG / `azd down --purge`, purge soft-deleted resources, quota increase requests, enabling public network access, deleting other deployments | never executed; documented manual steps only |

The az runner rejects any command not on the read-only allow-list and any argument containing shell metacharacters
(`az` is a `.cmd` shim on Windows). Every command is validated before any of them runs. Secrets are redacted by key
name and value pattern. Any `{"name", "value"}` pair (App Service settings) has its value replaced at the runner layer,
so reports, excerpts and recordings only show set/empty/placeholder state. Git diff evidence hides `.env` values and
secret-named keys. Recorded fixtures are anonymized (subscription/tenant ids, UPNs, tenant names). The MCP server
exposes two gated tools, `apply_fix` and `chat_test`, which refuse without explicit user approval (see Copilot / MCP).
`az rest` is allowed for exactly two **GET** reads (the Application Insights component in ARM, and the Application
Insights query API); `--body`, `--headers`, other hosts, other resource types and other methods are rejected. The chat
test's HTTP calls never follow redirects (that would forward the token), only go to the host Azure reports for the API
app (never a URL from a local file), refuse tokens containing whitespace, and keep the token out of every log, report
and error message. The test session is deleted after the message, even after a timeout; a failed delete is reported
with the session id so you can remove it by hand.

**Policy exemption tag (`apply-exemption-tag`)** is applied in stages because Azure applies tags and network changes
asynchronously: (1) tag the resource group and the Cosmos DB account, (2) poll `az tag list` until the tag is visible
everywhere and `az cosmosdb show` says the account is no longer *Updating*, plus a 30 s settle pause, (3) enable public
access (retrying a "busy" account up to 3 times, 60 s apart), (4) poll until it reports *Enabled*, wait 45 s and check
the policy did not revert it, then (5) re-run the health check. It stops at the first step that fails or times out and
prints a short numbered "what to do now" list; every step is safe to repeat. Expect 5-15 minutes.

## Quota workflow (end to end)

The quota numbers come **live from your Azure subscription**, not from the docs: for every region checked the tool runs
`az cognitiveservices usage list --location <region>` (used / limit per model and deployment type, as Azure reports it
for the signed-in subscription), and `az cognitiveservices model list --location <region>` for what the region
supports. The docs and Bicep are only used for what the deployment *needs* and for which regions the template allows.
Every report prints its source ("live 'az cognitiveservices usage list' in N region(s), subscription …; need from
infra/main.bicep:98 default").

1. **Requirements (what the deployment needs):** model, SKU and capacity per deployment, read from your repo **on every
   run** (nothing is cached), using the same order azd uses, highest first: what-if override (`--capacity`) → azd env
   variable (e.g. `AZURE_ENV_GPT_MODEL_CAPACITY`) → `.azure/<env>/config.json` parameter → a value or `${VAR=default}`
   written in `infra/main.parameters.json` → the default in `infra/main.bicep`. So if you edit the parameter file, the
   Bicep file or the azd env, the next run uses it, and each number is printed with the file and line (or env variable)
   it came from.
2. **Live quota:** `az cognitiveservices usage list` for the target region. If that region is short, all regions the
   template allows (`@allowed` on the AI location parameter) are checked too.
3. **Availability:** model name, version and deployment type, plus retirement date (`az cognitiveservices model list`).
4. **Consistency:** Bicep capacity vs the azd `usageName` region-picker filter, `QuotaCheck.md`, `checkquota.sh` and
   `quota_check_params.sh` defaults and regions.
5. **Recommendation:** lower the capacity to what's available, or use a region where every model fits. The agent knows
   that **Global**-scope quota (e.g. `gpt-5.4-mini` GlobalStandard) is a single subscription-wide pool, so switching
   region won't help there, unlike **Classic** per-region quota (e.g. `gpt-realtime-mini`).
6. **Fix and verify:** `fix set-model-capacity --approve` runs `azd env set …`, then re-checks quota live.

## Copilot / MCP

`.vscode/mcp.json` registers the stdio server `accelerator-troubleshooter`
(`python troubleshooter/mcp_launcher.py`, with no `cwd` or `${workspaceFolder}` so it starts from any host; requires
`pip install -r troubleshooter/requirements-mcp.txt`). Its tools are
`identify_accelerator`, `about_accelerator`, `search_docs`, `health_check`, `diagnose`, `quota_check`, `preflight`,
`doc_drift`, `draft_guide_entry`, `resume_plan`, `cleanup_plan` and `list_checks` (all read-only; `draft_guide_entry` only
prints a draft, and the resume and cleanup tools only print commands for you to run) plus two
gated tools that act for the user and refuse unless the user approved that exact action: **`apply_fix`** (one
allow-listed fix) and **`chat_test`** (one real chat message, see "Real chat test"). Every tool runs the same CLI in a
fresh process, so **chat and terminal always give the same findings**. In VS Code, pick the **accelerator-troubleshooter**
custom agent in Copilot Chat and
say hi (the agent replies with a grouped menu of the nine questions below), or type `/` and use one of them (skills in
`.github/skills/`; type the command, then your text):

| Slash command | When | What you get |
|---|---|---|
| `/about-accelerator` | first | accelerator name, 2-3 lines from its README (with file:line), Azure services, docs searched first |
| `/preflight` | before `azd up` | GO / NO-GO, recommended region, quota needed vs free (live from your subscription), the tools and versions the docs ask for versus what is installed, blockers |
| `/health-check [rg-name]` | after `azd up` | health of the current azd env, or of any resource group you name, including what the apps logged and the Fabric capacity state (Active / Paused) |
| `/prove-chat-works` | after a healthy check | asks first, then sends ONE real chat message with your sign-in and deletes the test session |
| `/resume-deployment [rg-name]` | `azd up` stopped halfway | where it stopped and whether a re-run is safe: NOTHING TO RESUME, WAIT, FIX FIRST, SAFE TO RESUME; the exact command, never run for you |
| `/cleanup-resources [rg-name]` | before `azd down` | what would be deleted, locks and soft-deleted resources that need a purge, the accelerator's own teardown (`predown` hook, script, doc lines) and the commands; deletes nothing |
| `/why-is-this-failing <error>` | on an error | the accelerator's docs first, then live checks, then (only if no solution) wider sources; every statement names its source (📘 docs path:line, 🔬 live check id, 🌐 external URL) |
| `/draft-guide-entry <error>` | after fixing something new | a ready-to-paste, generalised troubleshooting-guide row with its section and line (prints only) |
| `/doc-drift` | maintainers | where the docs and scripts disagree with the Bicep and parameter files, with file, line and fix; also saves `.runs/document-gaps.md` |

The terminal equivalents are `about`, `preflight`, `health`, `chat-test`, `resume`, `cleanup`, `diagnose`, `draft-guide` and `drift`. When
`diagnose` finds no documented cause it
ends with "NOT IN THE ACCELERATOR DOCS?" and links to a GitHub-issues search and Microsoft Learn, labelled as external.

After you update the troubleshooter code, restart the MCP server once so chat does not keep running an older copy
(Command Palette → **MCP: List Servers** → `accelerator-troubleshooter` → **Stop Server**, then Start; it only starts
once a chat session exists, so send one message first), then run **Developer: Reload Window**.

## Knowledge index

`python -m accel_troubleshooter index --export knowledge` writes [`knowledge/INDEX.md`](knowledge/INDEX.md): every
documented error code per accelerator with category, services, commands and an exact `path:line` reference at the
indexed commit, plus document coverage gaps and broken local links. The runtime index is cached in `.cache/` and
rebuilt automatically when any indexed file's hash changes or a new document appears (`index` shows freshness).

**Every document is read** (`docscan.py`), whether or not a profile lists it: all `.md`/`.mdx`/`.rst` files in the
repository (and `.txt` under a docs folder), skipping vendored folders, the troubleshooter's own files and boilerplate
(license, conduct, security, templates). Notebooks and files over 400 KB are listed as not read, never silently dropped.
Troubleshooting guides and READMEs get extra attention:
- a guide is recognised by its name *or* its content (a table of errors with resolutions), so a vaguely named file is
  still found; a generic FAQ heading in an unrelated document is not enough;
- a guide that is a `| **Code** |` table, any other error table, or plain question headings (FAQ style) is split into
  one searchable entry per row or heading, each with its file and line;
- every README in the repository is kept, and a "Troubleshooting" or "Known issues" section of *any* document is
  ranked higher than an ordinary mention (guide sections x1.3, READMEs x1.15);
- `python -m accel_troubleshooter docs` prints what was read per kind, how each guide parsed, what the root README
  promises (prerequisites, regions, models, commands) and what was skipped and why. `index` has the full JSON.

## Tests and evaluation

```powershell
python -m venv --system-site-packages .venv; .\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest            # unit + integration (+ MCP handshake when mcp is installed)
.\.venv\Scripts\python -m flake8 --config .flake8 accel_troubleshooter tests
python -m accel_troubleshooter eval         # 20 labelled scenarios; accelerator/stage/top-cause/doc/action/grounding metrics
```

Fixtures in `evals/fixtures/`:

- `live-*.json` are **anonymized recordings of real read-only az calls** made while building this tool. They cover a
  deleted RG, the healthy quota state across 5 regions, and a quota what-if.
- `synthetic-*.json` are **synthetic** (a healthy chatbot deployment `rg-demo`). Each eval case injects one fault into
  them (placeholder images, missing agent settings, missing RBAC, InsufficientQuota operation, 429, disabled public
  access, unregistered provider, stuck deployment, deployer Cosmos role, deleted app setting, unsupported model version).

## Observability

Each diagnosis is appended to `.runs/runs.jsonl` (accelerator, stage, codes, per-check status/duration/commands, top
hypothesis, cited docs). `python -m accel_troubleshooter stats` aggregates it.

## Using it in another accelerator

The tool needs only Python 3.10+ and the Azure CLI. One command copies everything (the tool, the Copilot agent, the
nine starter-question skills and the MCP entry) and tells you whether the target repo is recognised:

```powershell
# from this repo's troubleshooter folder; add --dry-run first to preview, --force to update an existing install
python -m accel_troubleshooter install C:\Repo\content-processing-solution-accelerator
```

Then, in the other repository:

1. `pip install -r troubleshooter/requirements-mcp.txt` (once; only needed for chat, not for the terminal).
2. Terminal: `cd troubleshooter` then `python -m accel_troubleshooter preflight` (or `health`).
3. Chat: VS Code → **Developer: Reload Window** → new chat → pick **accelerator-troubleshooter** → send any message
   (the MCP server only starts once a chat session exists) → say "hi" for the starter questions.
4. Commit `.github/agents/`, `.github/skills/`, `.vscode/mcp.json` and `troubleshooter/` (the tool's own `.gitignore`
   keeps `.runs/` and `.cache/` out).

What it copies: `troubleshooter/` (runtime only; add `--full` for tests, evals and the demo script),
`.github/agents/accelerator-troubleshooter.agent.md`, `.github/skills/<9 skills>/SKILL.md`, and the
`accelerator-troubleshooter` server merged into `.vscode/mcp.json` (other servers are kept; a file with comments is left
alone and the snippet is printed). Existing files are never overwritten without `--force`.

The accelerator is recognised from `azure.yaml`, the git remote and marker files, and the repo in the current workspace
is always tried first, whatever the folder is called. The three shipped profiles (Customer Chatbot, Content Processing,
Chat With Your Data) work immediately. **Any other azd/Bicep accelerator also works with no setup:** when no shipped
profile matches the workspace, the tool derives one from the repository itself (`autoprofile.py`): name and remote from
`azure.yaml` and git, docs from `README.md` and `docs/` grouped by file name (troubleshooting, deployment, quota, auth, ...),
model quota from the Bicep `usageName` metadata, parameter names and azd keys from `main.bicep` and
`main.parameters.json`, and required providers from the Bicep resources and AVM modules. Docs search, `preflight` (quota and
model availability) and the generic Azure checks then work; app-level checks do not (they need an `apps` section).
`python -m accel_troubleshooter init-profile` saves the derived profile as `profiles/<id>.json` so you can review, correct
and commit it; a saved profile then takes over from the derived one. A folder with neither `azure.yaml` nor
`infra/main.bicep` is not an accelerator, so the tool asks instead of guessing.

Manual alternative: copy the `troubleshooter/` folder and the files above by hand.

`preflight` prints **NOT CHECKED** (exit code 3), never GO, when no live quota check could run (offline, not logged
in, or the repo could not be found).

## Four more capabilities

### Optional sign-in step (Microsoft Entra), part of every health check
`auth.setup` reads only the *names* of the app settings `ENTRA_AUTH_CLIENT_ID` / `ENTRA_AUTH_TENANT_ID` on both backends
(never values). Set on all: pass. Missing on all: info, because the guide marks it optional (DeploymentGuide 5.4), and the
report adds an **OPTIONAL NEXT STEP** that names `infra/scripts/post-provision/configure_auth.ps1` / `.sh` and
`documents/AppAuthentication.md`, saying to run it after the post-provision steps (images, data, agents) and to re-run
the health check. Set on only some: warning ("re-run the script"). It is a manual step: the agent never runs it, because
it creates or updates an app registration. The profile key is `auth_setup`; other accelerators add their own.

### App error logs (Application Insights), part of every health check
`appinsights.errors` reads what the apps themselves logged in the last hour (`--window PT15M`, `PT6H`, `P1D` to change
it): exceptions, error-level logs, the accelerator's own `Error_*` events (for example `Error_Config_Missing`,
`Error_Agent_Rate_Limit`) and HTTP 5xx requests. Errors are grouped (ids and numbers normalised), counted, given a
latest-seen time and classified: `cosmos-firewall`, `model-not-found`, `throttled`, `rbac-denied`, `auth-token`,
`config-missing`, `timeout-network`, `other`. A category already explained by another finding (for example the Cosmos
firewall error when public access is disabled by policy) is added to that finding as evidence instead of being
reported twice; an unexplained one becomes its own finding with the closest guide rows as keyword matches. It uses two
read-only `az rest` GET calls (component → AppId, then the query API with a table name only), needs Reader on the
Application Insights resource, and says so plainly when monitoring is off or unreadable. Errors older than a fix stay
in the window, so the report shows when each was last seen.

### Real chat test (`chat-test`, chat tool `chat_test`)
Health probes only prove the apps answer "alive" (the chat API's `/health` never calls Cosmos). `chat-test` runs the
full health check plus **one real message** through the path a user takes: sign-in token, `POST /api/chat/message`,
Cosmos DB, Foundry agents, model. It is **opt-in and approval-gated** (terminal: asks you to type `yes`, or `--yes`;
chat: the agent asks yes/no, then calls `chat_test` with `user_approved=true`) because it writes one chat session and
uses a little model quota; the test session is deleted afterwards. The token comes from your own `az login`
(`az account get-access-token --scope api://<client-id>/user_impersonation`, client id from
`AZURE_ENV_ENTRA_CLIENT_ID` or `--client-id`) or from an environment variable (`--token-env NAME`). It is held in memory
only and never logged, recorded or put in a report. If Azure CLI is not allowed to request tokens for the API, the
result says how to fix it (add Azure CLI as a pre-authorised client of the app registration, or use `--token-env`).
Failures are classified (401 sign-in, 503 missing configuration, 429 throttling, Cosmos firewall, role denied, model
deployment missing, timeout) and back up the matching finding. Other accelerators need a `chat_test` block in their
profile (see `profiles/customer-chatbot.json`).

### Documentation drift (`drift`, chat tool `doc_drift`)
Offline and read-only. Compares the docs and scripts with `infra/main.bicep` and `infra/main.parameters.json`:
documented default values vs the template default (resolved the way azd does), documented allowed values and minimums,
variables documented but never read or read but never documented, regions and models in the quota docs and scripts
that the template does not use, broken relative links (GitHub root-relative links resolve from the repo root) and
scripts a doc tells you to run that do not exist. Every finding names the file and line and how to fix it;
`drift --format full --out drift.md` writes a PR-ready checklist. Doc findings are low severity: they never become a
"cause" in a health check.

Every `drift` run (and `/doc-drift` in chat) also saves **`troubleshooter/.runs/document-gaps.md`**, a local,
git-ignored list of everything the documentation is missing or gets wrong: the drift findings, documents that ask for
**different versions of the same tool** (for example Python 3.9+ in one and 3.11 in another), a missing troubleshooting
guide, guides with no recognisable entries, a README without a prerequisites section, documents the profile expects
but the repository lacks, and broken links. `drift --no-save` skips the file.

### Tools and versions (preflight, `repo.tools`)
`preflight` also compares what the documents ask for with what is installed on this machine (`azd`, `az`, `python`,
`node`, `pwsh`, `docker`, `git`, `dotnet`, `terraform`, `kubectl`; fixed read-only `--version` calls, nothing else is
run). Minimum versions come from `azure.yaml` `requiredVersions` (azd refuses to run below it: a blocker) and from
prerequisite sections and version mentions in the README and deployment guides, each with its file and line. A missing
`azd`/`az` is a blocker; other missing or old tools are warnings, and a tool the docs call optional (for example Docker
for the Dev Container route) is only informational. `az extension add --name X` lines in docs and scripts are checked
against `az extension list`. Other tools found on the machine are listed as "also installed".

### Fabric capacity (`azure.fabric`, part of every health check)
For accelerators that deploy a `Microsoft.Fabric/capacities` resource (or record `AZURE_FABRIC_CAPACITY_NAME`), the
health check reads its state with `az resource show`: **Active** passes, **Paused** is a confirmed cause (Fabric items
are unavailable until it is resumed) with a resume command shown but never run (billing restarts), **Suspended** or
**Failed** is a failure, transitions are informational. It is skipped quietly when there is no capacity. Fabric
workspace and item checks (REST API) are not built yet.

### Resume a halted deployment (`resume`, chat tool `resume_plan`)
Azure cannot continue a half-finished ARM deployment, but `azd provision` re-applies the template and only creates
what is missing, so a re-run is usually safe once the cause of the stop is gone. `resume` reads the deployment
history, resource states, the post-provision state and the Fabric capacity and says: **NOTHING TO RESUME**, **WAIT**
(a deployment is still running; stuck for over an hour gets a cancel command), **FIX FIRST** (a repeatable cause such
as quota, region, policy or a resource in a Failed state: re-running would stop at the same place), **SAFE TO RESUME**
(`azd provision`) or **SAFE TO RESUME THE LAST STEP** (infrastructure is complete, the post-provision step is not:
`azd hooks run postprovision` when `azure.yaml` defines it). Where the post-provision step cannot be verified it says
so and never claims it finished. It runs nothing.

### Guided cleanup (`cleanup`, chat tool `cleanup_plan`)
Read-only and advisory: nothing is deleted by the tool or the agent. It lists what a cleanup would delete (resources by
type), resource **locks** that block deletion, resources not tagged for this azd environment (deleting the group
deletes them too, only reported when the group uses that tag), services Azure keeps after deletion and the exact
purge commands (Cognitive Services, Key Vault, API Management, App Configuration), what the accelerator itself says
(the `predown` hook in `azure.yaml`, teardown scripts, teardown lines in the documents, each with file and line), and the
steps: Option A `azd down --purge --force` (which also runs the `predown` hook) and Option B delete the group then
purge, with a note on what B skips. A Fabric capacity bills while Active even when idle: pausing it is the cheaper
alternative when you want to keep the data.

**Deleting the resource group for you (`cleanup --delete`, chat tool `delete_resource_group`).** The only destructive
operation, kept out of the approved-fix path (`apply_fix` cannot reach it), and it deletes exactly one resource group:
1. It reads the group again from Azure: it must exist, be in the signed-in subscription (and in the azd environment's
   subscription when it is that environment's group) and have no locks; otherwise it stops. It warns when the group
   belongs to another azd environment and when a plain group delete skips the accelerator's `predown` hook (for example
   Microsoft IQ's Fabric clean-up: use `azd down` for that). It prints a fingerprint of what you were shown
   (subscription, group and every resource id).
2. You type the exact group name (case-sensitive). Then you type `delete <name>`. Anything else deletes nothing.
3. Right before deleting it reads the group again; if anything was added or removed, or the subscription changed, the
   fingerprint differs and it refuses.
4. It runs one command, `az group delete --name <name> --yes --no-wait`, waits until Azure reports the group gone (up to
   35 minutes, otherwise it says "still deleting"), lists soft-deleted AI accounts from that group with purge commands
   for you to run (it never purges), and appends a record to `.runs/deletions.jsonl`.

In a terminal: `python -m accel_troubleshooter cleanup --delete --resource-group <name>` (asks both questions). In chat
the agent asks you to type the name, then the phrase, in two separate messages, and passes exactly what you typed.

### Draft a guide entry (`draft-guide`, chat tool `draft_guide_entry`)
Turns a diagnosed incident into one row in the guide's own table format
(`| **Error** | Description | <ul><li>steps</li></ul> |`), generalised (resource names, ids, emails, addresses and
secrets replaced by placeholders), with how to confirm it (the read-only commands the checks ran), the cause, the fix
steps, your `--note` ("verified fix") and references. It says which section the row belongs to and after which line
to insert it, and refuses with a pointer when the guide already documents the error code (`--force` overrides). It
**only prints**; nothing is written to the guide unless you ask the chat agent to insert it.

## Test it end to end (five scenarios taken from the troubleshooting guide)

Each scenario can be reproduced safely and each is documented in `documents/TroubleShootingSteps.md`. Run them from
`troubleshooter/`; to try a bad value **without touching your azd environment**, put it in a throwaway file and pass
`--env-file` (for example `AZURE_ENV_GPT_MODEL_CAPACITY="5000"`).

| # | Scenario (guide entry) | How to manufacture it | What the agent should say |
|---|---|---|---|
| 1 | Quota too high (`InsufficientQuota`, Quota & Capacity) | `azd env set AZURE_ENV_GPT_MODEL_CAPACITY 5000`, then `preflight`; optionally `azd up` (it fails at validation, nothing is created) | NO-GO before deploying, with `need 5000 / free <n>`; after a failed `azd up`, a confirmed cause using the numbers from Azure's own error |
| 2 | Deleted or stale resource group (`ResourceGroupNotFound`) | run `health` with an azd env whose resource group was deleted; or paste `ResourceGroupNotFound: Resource group 'rg-x' could not be found.` | confirmed (live) or likely (pasted only), cites the guide row, suggests a new azd env |
| 3 | Model version not offered in the region (`DeploymentModelNotSupported`, Regional) | `azd env set AZURE_ENV_GPT_MODEL_VERSION 2099-01-01`, then `preflight` | NO-GO: version not offered in the region, lists the versions that are |
| 4 | Parameter value not allowed (`InvalidTemplate`, Regional/Config) | `azd env set AZURE_ENV_AI_SERVICE_LOCATION westus`, then `preflight`; or paste `The provided value 'westus' for the template parameter 'azureAiServiceLocation' ... is not valid` | the region is not in the Bicep `@allowed` list (file:line), and the command to fix it |
| 5 | Post-deployment steps skipped / app stopped (Deployment Guide 5.3) | after `azd up`, do not run the image build and data/agents scripts, then run `health`; or `az webapp stop` the API app | placeholder images / missing agent settings named with the script to run; a stopped app gets an approval-gated `start-webapp` fix |

Also worth trying: paste any error code from the guide's "Most frequently encountered errors" table; the agent matches
the guide's own row and shows its steps, and when the guide has no entry it says so and links external searches.

**Preflight to post-deployment in two terminals**

1. Terminal A: `python -m accel_troubleshooter preflight` (GO or NO-GO, recommended region, blockers).
2. Terminal B: `azd up` (or `python -m accel_troubleshooter run -- azd up` to diagnose automatically on failure).
3. Terminal A: `python -m accel_troubleshooter watch`. It prints one line per change and stops with a full diagnosis at
   the first failed operation, even while ARM still reports the deployment as running.
4. Terminal B: the post-provision scripts from the deployment guide (build images, data and agents).
5. Terminal A: `python -m accel_troubleshooter health`; chat works the same way (`/preflight`, `/health-check`).

## Demo playbook (about 25 minutes; parts 1 and 2 need no deployment)

**Part 1 - no Azure resources needed (5 min).** Terminal in `troubleshooter/`:
1. `python -m accel_troubleshooter about` (or chat: "hi", then `/about-accelerator`): what the accelerator is, from its README.
2. `python -m accel_troubleshooter preflight`: GO / NO-GO, recommended region, live quota, the `SecurityControl` tag reminder.
3. `python -m accel_troubleshooter drift`: real doc-vs-template mismatches with file and line;
   `drift --format full --out drift.md` for a PR-ready checklist.

**Part 2 - the troubleshooting guide's errors, pasted (5 min).** For each, run
`python -m accel_troubleshooter diagnose "<paste>"` (or paste it after `/why-is-this-failing` in chat):
1. `ResourceGroupNotFound: Resource group 'rg-old' could not be found.` (guide row, suggests a new azd env)
2. `InsufficientQuota: This operation require 50 new capacity in quota Tokens Per Minute ... available capacity 20`
3. `DeploymentModelNotSupported ... Version:2099-01-01` (model/region check, lists the versions that exist)
4. `The provided value 'westus' for the template parameter 'azureAiServiceLocation' is not valid` (Bicep `@allowed`, file:line)
5. `SpecialFeatureOrQuotaIdRequired: ... model 'Name:o3'` (guide row, access-request form)
6. An error the guide does not have (for example `ZxQuuxWidgetError`): the agent says the guide has no entry and links
   external searches; then `draft-guide "<same text>" --note "what fixed it"` shows the row it would add.

**Part 3 - fresh deployment, two terminals (15 min).**

| Step | Terminal A (agent) | Terminal B (deployment) |
|---|---|---|
| 1 | `preflight` (or `/preflight`): GO + region | |
| 2 | | `azd env new demo1`, `azd env set AZURE_ENV_AI_SERVICE_LOCATION <region from preflight>`, `azd up` |
| 3 | `watch`: one line per change, a full diagnosis at the first failed operation | (azd runs) |
| 4 | | post-provision: build images, then data and agents (Deployment Guide 5.3) |
| 5 | `health` (or `/health-check`): apps, RBAC, quota, **app error logs**, network | |
| 6 | If a Cosmos/policy problem shows: answer **yes** to the tag fix, watch tag → wait → enable → re-check | |
| 7 | `chat-test` (or "prove chat works" in chat): ONE real message; says where it breaks if it does | |
| 8 | Break something on purpose: `az webapp stop -g <rg> -n <api-app>`, then `health`; **yes** to start it; `health` again | |
| 9 | `draft-guide "chat did not work after the policy tag" --note "tagged the RG and enabled Cosmos"` | |

Cleanup: `azd down --purge --force`. Everything the agent runs is read-only except the three things you approve with
**yes**: a fix, the chat test, and (in chat only) inserting a drafted guide row.

## Demo scenarios (all reversible, all verified live on a real deployment)

| # | Break | Ask | The agent shows |
|---|---|---|---|
| 1 | `az webapp stop -g <rg> -n <api-app>` | `diagnose "the chat is not responding"` | confirmed: App Service stopped, HTTP 403 probe; then `fix start-webapp --approve` starts it and verifies fail→pass |
| 2 | blank one setting: `az webapp config appsettings set -g <rg> -n <api-app> --settings COSMOS_DB_ENDPOINT=` | `diagnose "I changed a setting and chat stopped"` | the exact missing setting + the Bicep lines that declare it (restore the value afterwards) |
| 3 | submit a deployment requesting 5000K TPM of the chat model on the AI account (Azure rejects it, nothing is created) | `diagnose "my deployment failed"` (no error pasted) | confirmed InsufficientQuota read from the failed ARM operation, with requested vs available numbers (delete the failed deployment record afterwards) |
| 4 | nothing to break: tenant policy | `diagnose "chat does not work, network?"` | confirmed: Azure Policy `CosmosDB_PublicNetwork_Modify` (found with `az policy state list`) turned Cosmos public access off because the resource and its resource group lack the exemption tag the policy's own rule names (`SecurityControl=Ignore`, read from the policy definition). Non-WAF flavor, so it did not come from the template. Options: 🟡 add the tag + re-enable public access (dev/test only; the agent asks yes/no, applies it, then re-runs the health check), 🔴 ask the policy owner for an exemption, 📘 deploy the WAF flavor instead |

## Extending to another accelerator

Usually nothing is needed: an azd/Bicep accelerator with no shipped profile gets one derived from its repository (see
"Using it in another accelerator"); `init-profile` saves it. Write or edit a profile by hand when you want more than the
derived one (app-level checks, resource-name patterns, a policy tag, a chat test):

1. Add `accel_troubleshooter/profiles/<id>.json`, copying an existing profile (or run `init-profile`). Fill in the fingerprint, docs per
   category, `iac.models` (Bicep parameter names) and `azd_env_keys`.
2. Run `python -m accel_troubleshooter index --accelerator <id>` and check `coverage` for missing or unindexed docs.
3. Optional: add `apps`/`expected_roles`/`post_provision` for app-level checks, new checks in `diagnostics/`
   (`@check`), new rules in `rca.py` (`@rule`), and new phrasing in `taxonomy.json` (no code change needed).
4. Add eval cases in `evals/cases.json`.
