# Enable the STANDING AGENDA on the GLOBAL Hermes registration (round 23).
# Adds agenda_list + agenda_add + agenda_update to MINERVA_TOOLS so the daily
# brainstorm ticks can SEE and MOVE her persistent task queue (the chat lanes
# carry these tools already, in code). Agenda content is HER OWN queue file in
# state/ - no operator-private data rides the free tick model because of this.
# SET-UNION (appends missing names; never removes what other enable scripts
# added). Backs up config.yaml first. Idempotent. Prints its proof.
$ErrorActionPreference = "Stop"
$H = Join-Path $env:LOCALAPPDATA "hermes"
$cfg = Join-Path $H "config.yaml"
if (!(Test-Path $cfg)) { Write-Host "Hermes config not found at $cfg - is Hermes installed?"; exit 1 }

$want = @("agenda_list", "agenda_add", "agenda_update")
$raw = [IO.File]::ReadAllText($cfg)

$m = [regex]::Match($raw, "MINERVA_TOOLS:\s*([^`r`n]*)")
if (-not $m.Success) {
  Write-Host "Could not find MINERVA_TOOLS in the config - the minerva MCP registration"
  Write-Host "looks different than expected. Nothing changed. Check the config by hand."
  exit 1
}

$have = $m.Groups[1].Value.Trim() -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$missing = $want | Where-Object { $have -notcontains $_ }

if (-not $missing) {
  Write-Host "Already enabled - nothing to change."
} else {
  $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
  Copy-Item $cfg "$cfg.$Stamp.agenda.bak"
  attrib +R "$cfg.$Stamp.agenda.bak"
  Write-Host "backed up: config.yaml.$Stamp.agenda.bak"
  $newList = ($have + $missing) -join ","
  $raw = $raw -replace "MINERVA_TOOLS:[^`r`n]*", "MINERVA_TOOLS: $newList"
  [IO.File]::WriteAllText($cfg, $raw)
  Write-Host "updated (added: $($missing -join ', '))."
}

Write-Host ""
Write-Host "The minerva registration env now reads:"
(Get-Content $cfg) | Where-Object { $_ -match "MINERVA_" } | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Plainly: her brainstorm ticks now SEE her standing agenda and can"
Write-Host "move it - record progress, finish items (a 'done' needs a note"
Write-Host "saying what actually happened), and queue up to 2 new standing"
Write-Host "items a tick (adopted pitches especially). Working the agenda"
Write-Host "does not use up her one pitch. Caps: 25 open items max, near-"
Write-Host "duplicates refused. 'Minerva - Agenda' shows the queue anytime."
