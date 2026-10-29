<#
.SYNOPSIS
  Reproducible end-to-end demo of the accelerator troubleshooting agent.

.DESCRIPTION
  Part 1 (default, no Azure needed): identification, doc retrieval, and replayed diagnoses of
          recorded-live and synthetic fault scenarios, plus the evaluation suite.
  Part 2 (-Live): read-only live checks against your subscription (quota workflow + preflight),
          then a real fix -> verify loop executed in a scratch copy under %TEMP% (never in this repo).
  Reports are written to troubleshooter/demo-output/.
#>
param([switch]$Live, [string]$Subscription)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$out = Join-Path $root 'demo-output'
New-Item -ItemType Directory -Force $out | Out-Null
$fx = Join-Path $root 'evals/fixtures'
Push-Location $root
function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ts { python -m accel_troubleshooter @args; Write-Host "(exit ${LASTEXITCODE}: 0=no root cause, 1=findings, 2=clarification needed)" -ForegroundColor DarkGray }

Step '1. Accelerator identification (workspace, text, ambiguity)'
Ts identify "my deployment failed with a quota exceeded error"
Ts identify "Chat with your data is deployed, but my application cannot access Azure OpenAI"
Ts --workspace $fx identify "my app is broken"

Step '2. Grounded retrieval from the real troubleshooting guide'
Ts search "InsufficientQuota" --accelerator customer-chatbot -k 2

Step '3. Recorded LIVE scenario: endpoint errors, but the resource group was deleted'
Ts diagnose "My deployment succeeded, but the endpoint is returning 500 errors" --replay "$fx/live-rg-deleted.json" `
   --resource-group rg-pgbyocc4 --region eastus2 --out "$out/01-rg-deleted.md"

Step '4. Recorded LIVE quota workflow (what-if: 800K TPM gpt-5.4-mini)'
Ts diagnose "azd up failed: InsufficientQuota - This operation require 800 new capacity in quota Tokens Per Minute (thousands) - gpt-5.4-mini" `
   --replay "$fx/live-quota-shortfall.json" --region swedencentral --capacity gptDeploymentCapacity=800 `
   --capacity gptRealtimeDeploymentCapacity=20 --out "$out/02-quota-shortfall.md"

Step '5. SYNTHETIC fault: post-provision agents not created'
python -c "import json;d=json.load(open('$($fx -replace '\\','/')/synthetic-base.json'));k='webapp config appsettings list --resource-group rg-demo --name api-chat-demo';[s.update(value='') for s in d['commands'][k]['stdout'] if s['name'].startswith('FOUNDRY_')];json.dump(d,open('$($out -replace '\\','/')/agents-missing.json','w'))"
Ts diagnose "The chatbot UI loads but every chat message returns a 500 error" --replay "$out/agents-missing.json" `
   --env-file "$fx/synthetic-env.txt" --out "$out/03-agents-missing.md"

Step '6. Regression / evaluation suite'
Ts eval

if ($Live) {
  Step '7. LIVE read-only quota workflow across all allowed AI regions'
  Ts quota --out "$out/04-live-quota.md"
  Step '8. LIVE preflight (GO / NO-GO)'
  Ts preflight --out "$out/05-live-preflight.md"

  Step '9. LIVE fix -> verify loop in a scratch copy (azd env only, no Azure resources are changed)'
  $sub = if ($Subscription) { $Subscription } else { az account show --query id -o tsv }
  $demo = Join-Path $env:TEMP 'ts-demo\ccsa'
  Remove-Item (Split-Path $demo) -Recurse -Force -ErrorAction SilentlyContinue
  $repo = Split-Path $root
  foreach ($rel in 'azure.yaml','infra\main.bicep','infra\main.parameters.json','chat-app\backend\app\foundry_client.py',
                   'scenarios\scenario_loader.py','infra\scripts\pre-provision\checkquota.sh','infra\scripts\pre-provision\quota_check_params.sh') {
    $d = Join-Path $demo $rel; New-Item -ItemType Directory -Force (Split-Path $d) | Out-Null; Copy-Item (Join-Path $repo $rel) $d }
  Copy-Item (Join-Path $repo 'documents') (Join-Path $demo 'documents') -Recurse
  Push-Location $demo
  azd env new demo --subscription $sub --location australiaeast --no-prompt | Out-Null
  azd env set AZURE_ENV_AI_SERVICE_LOCATION swedencentral | Out-Null
  azd env set AZURE_ENV_GPT_MODEL_CAPACITY 5000 | Out-Null   # deliberately above the subscription's quota
  Pop-Location
  Ts --workspace $demo diagnose "azd up failed with InsufficientQuota for gpt-5.4-mini" --out "$out/06-live-before-fix.md"
  Ts --workspace $demo fix set-model-capacity --approve --issue "azd up failed with InsufficientQuota for gpt-5.4-mini" --out "$out/07-live-after-fix.md"
}
Pop-Location
Write-Host "`nReports: $out" -ForegroundColor Green
