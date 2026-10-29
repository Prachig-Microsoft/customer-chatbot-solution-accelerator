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
python -m accel_troubleshooter diagnose "My deployment failed with a quota exceeded error"   # live, read-only
python -m accel_troubleshooter quota                       # end-to-end quota workflow, all allowed AI regions
python -m accel_troubleshooter preflight                   # GO / NO-GO before azd up
python -m accel_troubleshooter search "InsufficientQuota"  # cited doc retrieval
python -m accel_troubleshooter eval                        # regression / evaluation suite (no Azure needed)
./demo.ps1                                                 # reproducible demo (add -Live for live Azure parts)
```

Useful options: `--resource-group`, `--region`, `--env <azd env>`, `--env-file <file>`, `--accelerator <id>`,
`--offline` (docs + repo only), `--replay <fixture.json>`, `--record <fixture.json>` (sanitized + anonymized),
`--capacity gptDeploymentCapacity=80` (what-if), `--full` (run every check), `--only a,b`, `--format json`, `--out file`.
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
| 🟡 approval required | `azd env set` capacity/region, `az provider register`, restart web app, rerun post-provision scripts, re-create a template-declared role assignment | only tool-generated commands on the executor allow-list (`EXECUTABLE_ACTIONS`) run, and only via `fix <id> --approve` + typed confirmation; verification checks re-run afterwards. Long-running/cost-incurring steps (`azd provision`, new env, log streaming) are shown but you run them yourself. |
| 📘 documented steps | resolution steps copied from the accelerator's troubleshooting guide | displayed with their doc reference; never executed (some are destructive, e.g. `azd down --purge`) |
| 🔴 restricted | delete RG / `azd down --purge`, purge soft-deleted resources, quota increase requests, enabling public network access, deleting other deployments | never executed; documented manual steps only |

The az runner rejects any command not on the read-only allow-list and any argument containing shell metacharacters
(`az` is a `.cmd` shim on Windows). Every command is validated before any of them runs. Secrets are redacted by key
name and value pattern. Any `{"name", "value"}` pair (App Service settings) has its value replaced at the runner layer,
so reports, excerpts and recordings only show set/empty/placeholder state. Git diff evidence hides `.env` values and
secret-named keys. Recorded fixtures are anonymized (subscription/tenant ids, UPNs, tenant names). The MCP server
exposes no execution tool.

## Quota workflow (end to end)

1. **Requirements:** model, SKU and capacity per deployment, resolved with the precedence what-if override → azd env var →
   azd `config.json` parameter → Bicep default, each with its source line.
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
(`python -m accel_troubleshooter.mcp_server`, requires `pip install -r requirements-mcp.txt`). Its tools are
`identify_accelerator`, `search_docs`, `diagnose`, `quota_check`, `preflight` and `list_checks`, all read-only.
In VS Code, pick the **accelerator-troubleshooter** custom agent in Copilot Chat and describe the problem.

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

## Extending to another accelerator

1. Add `accel_troubleshooter/profiles/<id>.json`, copying an existing profile. Fill in the fingerprint, docs per
   category, `iac.models` (Bicep parameter names) and `azd_env_keys`.
2. Run `python -m accel_troubleshooter index --accelerator <id>` and check `coverage` for missing or unindexed docs.
3. Optional: add `apps`/`expected_roles`/`post_provision` for app-level checks, new checks in `diagnostics/`
   (`@check`), new rules in `rca.py` (`@rule`), and new phrasing in `taxonomy.json` (no code change needed).
4. Add eval cases in `evals/cases.json`.
