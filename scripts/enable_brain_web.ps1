# Enable Brain recall + web/files tools on the GLOBAL Hermes registration (round 12).
# The global 'minerva' MCP registration's env decides what the Hermes lane (ticks +
# chat fallback) can see. Until now: ping,episodic_note,pitch_idea,notify_operator and
# NO Brain path. This adds brain_recall (READ-ONLY recall, per the brain_access lock)
# + web_fetch + files_* and sets MINERVA_BRAIN.
# Backs up config.yaml first (read-only timestamped copy). Idempotent.
$ErrorActionPreference = "Stop"
$H = Join-Path $env:LOCALAPPDATA "hermes"
$cfg = Join-Path $H "config.yaml"
if (!(Test-Path $cfg)) { Write-Host "Hermes config not found at $cfg - is Hermes installed?"; exit 1 }

$newTools = "ping,brain_recall,episodic_note,pitch_idea,notify_operator,web_fetch,files_list,files_read,files_write"
# HOST PATH: MINERVA_BRAIN, else a Brain folder in your home folder.
$brainPath = if ($env:MINERVA_BRAIN) { $env:MINERVA_BRAIN } else { Join-Path $HOME "Brain" }
$raw = [IO.File]::ReadAllText($cfg)

if ($raw -notmatch "MINERVA_TOOLS:") {
  Write-Host "Could not find MINERVA_TOOLS in the config - the minerva MCP registration"
  Write-Host "looks different than expected. Nothing changed. Tell Claude next session."
  exit 1
}

if (($raw -match [regex]::Escape("MINERVA_TOOLS: $newTools")) -and ($raw -match "MINERVA_BRAIN:")) {
  Write-Host "Already enabled - nothing to change."
} else {
  $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
  Copy-Item $cfg "$cfg.$Stamp.brainweb.bak"
  attrib +R "$cfg.$Stamp.brainweb.bak"
  Write-Host "backed up: config.yaml.$Stamp.brainweb.bak"
  $raw = $raw -replace "MINERVA_TOOLS:[^`r`n]*", "MINERVA_TOOLS: $newTools"
  if ($raw -notmatch "MINERVA_BRAIN:") {
    $raw = $raw -replace "(MINERVA_STATE_DIR:[^`r`n]*)", "`$1`r`n      MINERVA_BRAIN: $brainPath"
  }
  [IO.File]::WriteAllText($cfg, $raw)
  Write-Host "updated."
}

Write-Host ""
Write-Host "The minerva registration env now reads:"
(Get-Content $cfg) | Where-Object { $_ -match "MINERVA_" } | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Heads-up, plainly: with this ON, the Hermes lane's models (including the"
Write-Host "FREE Nous model the morning brainstorm runs on) can READ your Brain and"
Write-Host "fetch web pages during their sessions. Reading means that content can"
Write-Host "transit those providers. Brain WRITES stay impossible for engines."
Write-Host "If you ever want it off: restore the .bak listed above (remove its"
Write-Host "read-only flag first) or tell Claude to flip it back."
