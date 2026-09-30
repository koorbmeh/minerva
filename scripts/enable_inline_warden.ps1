# Enable inline warden for write-class tools (round 26).
# Sets MINERVA_INLINE_CLASSES=agenda_write,file_write in:
#   1. The Hermes global MCP registration (tick + Hermes chat lanes)
#   2. The Windows user environment (BYOK chat lane + any future subprocess)
# A haiku reviewer will now veto suspicious agenda_add/update and files_write
# calls BEFORE they run; a veto becomes an escalation to the operator (approve =
# one-time override; deny = standing denial; anti-nag dedupe applies).
# Backs up config.yaml first. Idempotent.
$ErrorActionPreference = "Stop"
$classes = "agenda_write,file_write"
$H = Join-Path $env:LOCALAPPDATA "hermes"
$cfg = Join-Path $H "config.yaml"
if (!(Test-Path $cfg)) { Write-Host "Hermes config not found at $cfg"; exit 1 }

$raw = [IO.File]::ReadAllText($cfg)
if ($raw -notmatch "MINERVA_STATE_DIR:") {
  Write-Host "Could not find MINERVA_STATE_DIR in the config - layout unexpected."
  Write-Host "Nothing changed. Tell Claude next session."
  exit 1
}

$already = $raw -match [regex]::Escape("MINERVA_INLINE_CLASSES: $classes")
$userAlready = [System.Environment]::GetEnvironmentVariable("MINERVA_INLINE_CLASSES", "User") -eq $classes

if ($already -and $userAlready) {
  Write-Host "Already enabled - nothing to change."
} else {
  # 1. Hermes config
  if (-not $already) {
    $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    Copy-Item $cfg "$cfg.$Stamp.inlinewarden.bak"
    attrib +R "$cfg.$Stamp.inlinewarden.bak"
    Write-Host "backed up: config.yaml.$Stamp.inlinewarden.bak"
    if ($raw -match "MINERVA_INLINE_CLASSES:") {
      $raw = $raw -replace "MINERVA_INLINE_CLASSES:[^`r`n]*", "MINERVA_INLINE_CLASSES: $classes"
    } else {
      $raw = $raw -replace "(MINERVA_STATE_DIR:[^`r`n]*)", "`$1`r`n      MINERVA_INLINE_CLASSES: $classes"
    }
    [IO.File]::WriteAllText($cfg, $raw)
    Write-Host "Hermes config updated."
  } else {
    Write-Host "Hermes config already has MINERVA_INLINE_CLASSES."
  }

  # 2. Windows user env (BYOK chat lane inherits this via host_loop)
  if (-not $userAlready) {
    [System.Environment]::SetEnvironmentVariable("MINERVA_INLINE_CLASSES", $classes, "User")
    Write-Host "Windows user env set: MINERVA_INLINE_CLASSES=$classes"
  } else {
    Write-Host "Windows user env already set."
  }
}

Write-Host ""
Write-Host "MINERVA_ env vars in Hermes config:"
(Get-Content $cfg) | Where-Object { $_ -match "MINERVA_" } | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "Inline warden is now ON for: $classes"
Write-Host "Classes: agenda_write (agenda_add/update) + file_write (files_write)"
Write-Host ""
Write-Host "What this means:"
Write-Host "  Before any agenda_add/update or files_write call is allowed, haiku"
Write-Host "  checks it looks legitimate. VETO -> escalation to you (approve or deny)."
Write-Host "  PASS -> runs as before. Timeout/error -> escalates (fail-safe)."
Write-Host "  Cost: ~$0.001 per reviewed call, only on those two classes."
Write-Host ""
Write-Host "Takes effect at the next Start Minerva restart."
