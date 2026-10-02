# Minerva lockdown apply — run AFTER install.ps1 + setup wizard. Idempotent. (2026-06-10)
# Backs up the live Hermes config, applies the merged lockdown config (engine memory OFF,
# curator OFF, wizard's Nous OAuth + free model PRESERVED), disables built-in toolsets,
# validates via hermes itself, and dumps MCP/dashboard recon for the gateway registration.
$ErrorActionPreference = "Stop"
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$H = Join-Path $env:LOCALAPPDATA "hermes"
# HOST PATH: MINERVA_HOME, else this script's own parent folder, else the current folder.
$P = if ($env:MINERVA_HOME) { $env:MINERVA_HOME }
     elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot }
     else { (Get-Location).Path }
$S = Join-Path $P "state"
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

Write-Host "`n== 1/5  Back up live config (read-only)" -ForegroundColor Cyan
Copy-Item "$H\config.yaml" "$H\config.yaml.$Stamp.minerva.bak"
attrib +R "$H\config.yaml.$Stamp.minerva.bak"
Write-Host "backed up: config.yaml.$Stamp.minerva.bak"

Write-Host "`n== 2/5  Apply Minerva lockdown config (memory OFF, curator OFF, Nous kept)" -ForegroundColor Cyan
Copy-Item "$P\config\hermes-config-minerva.yaml" "$H\config.yaml" -Force
Write-Host "applied"

Write-Host "`n== 3/5  Disable built-in toolsets (engine keeps ZERO tools until our gateway is registered)" -ForegroundColor Cyan
hermes tools list *> "$S\tools-before.txt"
foreach ($t in @("terminal", "web", "browser", "memory")) {
  try { hermes tools disable $t *>> "$S\tools-disable.txt" }
  catch { Write-Warning "disable '$t' failed - we'll check tools-disable.txt" }
}
hermes tools list *> "$S\tools-after.txt"
Write-Host "toolset states captured (before/after)"

Write-Host "`n== 4/5  Validate: hermes parses the new config" -ForegroundColor Cyan
hermes config *> "$S\h-config-after.txt"
hermes doctor *> "$S\h-doctor.txt"
Write-Host "config + doctor output captured"

Write-Host "`n== 5/5  Recon for MCP registration + API server" -ForegroundColor Cyan
hermes mcp add --help *> "$S\h-mcp-add.txt"
hermes mcp list *> "$S\h-mcp-list.txt"
hermes dashboard --status *> "$S\h-dash-status.txt"

Write-Host "`nDONE - the outputs are in state\" -ForegroundColor Green
