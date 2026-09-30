# Enable CONNECTION AWARENESS on the GLOBAL Hermes registration (round 14).
# Adds connections_status + request_connection to MINERVA_TOOLS so the morning
# brainstorm tick can SEE what Minerva is not connected to and ASK the operator for a
# connection (through the anti-nag pitch ledger).
# METADATA ONLY on this lane: the free tick model never gets email_read /
# calendar content tools here - actual mail/calendar reading stays on the paid
# chat lanes. SET-UNION (appends missing names; never removes what other
# enable scripts added). Backs up config.yaml first. Idempotent.
$ErrorActionPreference = "Stop"
$H = Join-Path $env:LOCALAPPDATA "hermes"
$cfg = Join-Path $H "config.yaml"
if (!(Test-Path $cfg)) { Write-Host "Hermes config not found at $cfg - is Hermes installed?"; exit 1 }

$want = @("connections_status", "request_connection")
$raw = [IO.File]::ReadAllText($cfg)

$m = [regex]::Match($raw, "MINERVA_TOOLS:\s*([^`r`n]*)")
if (-not $m.Success) {
  Write-Host "Could not find MINERVA_TOOLS in the config - the minerva MCP registration"
  Write-Host "looks different than expected. Nothing changed. Tell Claude next session."
  exit 1
}

$have = $m.Groups[1].Value.Trim() -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$missing = $want | Where-Object { $have -notcontains $_ }

if (-not $missing) {
  Write-Host "Already enabled - nothing to change."
} else {
  $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
  Copy-Item $cfg "$cfg.$Stamp.connaware.bak"
  attrib +R "$cfg.$Stamp.connaware.bak"
  Write-Host "backed up: config.yaml.$Stamp.connaware.bak"
  $newList = ($have + $missing) -join ","
  $raw = $raw -replace "MINERVA_TOOLS:[^`r`n]*", "MINERVA_TOOLS: $newList"
  [IO.File]::WriteAllText($cfg, $raw)
  Write-Host "updated (added: $($missing -join ', '))."
}

Write-Host ""
Write-Host "The minerva registration env now reads:"
(Get-Content $cfg) | Where-Object { $_ -match "MINERVA_" } | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Plainly: the morning brainstorm can now SEE what Minerva is not"
Write-Host "connected to (names + what each would unlock - metadata only, no"
Write-Host "mail or calendar content rides this free-model lane) and may ASK"
Write-Host "you for ONE connection as its pitch, anti-nag rules applying."
Write-Host "Your Discord 'never'/'later' answers are remembered as always."
