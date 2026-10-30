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
exposes one write tool, `apply_fix`, which refuses without explicit user approval (see Copilot / MCP).

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
`identify_accelerator`, `about_accelerator`, `search_docs`, `health_check`, `diagnose`, `quota_check`, `preflight` and
`list_checks` (all read-only) plus **`apply_fix`**, the one gated write tool: it refuses unless the user approved that
exact change and only runs allow-listed actions. Every tool runs the same CLI in a fresh process, so **chat and terminal
always give the same findings**. In VS Code, pick the **accelerator-troubleshooter** custom agent in Copilot Chat and
say hi, or type `/` and use one of the four ready-made questions (skills in `.github/skills/`; type the command, then
your text):

| Slash command | When | What you get |
|---|---|---|
| `/about-accelerator` | first | accelerator name, 2-3 lines from its README (with file:line), Azure services, docs searched first |
| `/preflight` | before `azd up` | GO / NO-GO, recommended region, quota needed vs free (live from your subscription), blockers |
| `/health-check [rg-name]` | after `azd up` | health of the current azd env, or of any resource group you name |
| `/why-is-this-failing <error>` | on an error | the accelerator's docs first, then live checks, then (only if no solution) wider sources; every statement names its source (📘 docs path:line, 🔬 live check id, 🌐 external URL) |

The terminal equivalents are `about`, `preflight`, `health` and `diagnose`. When `diagnose` finds no documented cause it
ends with "NOT IN THE ACCELERATOR DOCS?" and links to a GitHub-issues search and Microsoft Learn, labelled as external.

After you update the troubleshooter code, restart the MCP server once so chat does not keep running an older copy
(Command Palette → **MCP: List Servers** → `accelerator-troubleshooter` → **Stop Server**, then Start; it only starts
once a chat session exists, so send one message first), then run **Developer: Reload Window**.

## Knowledge index

`python -m accel_troubleshooter index --export knowledge` writes [`knowledge/INDEX.md`](knowledge/INDEX.md): every
documented error code per accelerator with category, services, commands and an exact `path:line` reference at the
indexed commit, plus document coverage gaps and broken local links. The runtime index is cached in `.cache/` and
rebuilt automatically when any indexed file's hash changes (`index` shows freshness).

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
four starter-question skills and the MCP entry) and tells you whether the target repo is recognised:

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
`.github/agents/accelerator-troubleshooter.agent.md`, `.github/skills/<4 skills>/SKILL.md`, and the
`accelerator-troubleshooter` server merged into `.vscode/mcp.json` (other servers are kept; a file with comments is left
alone and the snippet is printed). Existing files are never overwritten without `--force`.

The accelerator is recognised from `azure.yaml`, the git remote and marker files, and the repo in the current workspace
is always tried first, whatever the folder is called. The three shipped profiles (Customer Chatbot, Content Processing,
Chat With Your Data) work immediately. A **new** accelerator needs a profile file first
(`accel_troubleshooter/profiles/<id>.json`; see "Extending to another accelerator"); without one the tool asks which
accelerator you mean instead of guessing.

Manual alternative: copy the `troubleshooter/` folder and the files above by hand.

`preflight` prints **NOT CHECKED** (exit code 3), never GO, when no live quota check could run (offline, not logged
in, or the repo could not be found).

## Demo scenarios (all reversible, all verified live on a real deployment)

| # | Break | Ask | The agent shows |
|---|---|---|---|
| 1 | `az webapp stop -g <rg> -n <api-app>` | `diagnose "the chat is not responding"` | confirmed: App Service stopped, HTTP 403 probe; then `fix start-webapp --approve` starts it and verifies fail→pass |
| 2 | blank one setting: `az webapp config appsettings set -g <rg> -n <api-app> --settings COSMOS_DB_ENDPOINT=` | `diagnose "I changed a setting and chat stopped"` | the exact missing setting + the Bicep lines that declare it (restore the value afterwards) |
| 3 | submit a deployment requesting 5000K TPM of the chat model on the AI account (Azure rejects it, nothing is created) | `diagnose "my deployment failed"` (no error pasted) | confirmed InsufficientQuota read from the failed ARM operation, with requested vs available numbers (delete the failed deployment record afterwards) |
| 4 | nothing to break: tenant policy | `diagnose "chat does not work, network?"` | confirmed: Azure Policy `CosmosDB_PublicNetwork_Modify` (found with `az policy state list`) turned Cosmos public access off because the resource and its resource group lack the exemption tag the policy's own rule names (`SecurityControl=Ignore`, read from the policy definition). Non-WAF flavor, so it did not come from the template. Options: 🟡 add the tag + re-enable public access (dev/test only; the agent asks yes/no, applies it, then re-runs the health check), 🔴 ask the policy owner for an exemption, 📘 deploy the WAF flavor instead |

## Extending to another accelerator

1. Add `accel_troubleshooter/profiles/<id>.json`, copying an existing profile. Fill in the fingerprint, docs per
   category, `iac.models` (Bicep parameter names) and `azd_env_keys`.
2. Run `python -m accel_troubleshooter index --accelerator <id>` and check `coverage` for missing or unindexed docs.
3. Optional: add `apps`/`expected_roles`/`post_provision` for app-level checks, new checks in `diagnostics/`
   (`@check`), new rules in `rca.py` (`@rule`), and new phrasing in `taxonomy.json` (no code change needed).
4. Add eval cases in `evals/cases.json`.
