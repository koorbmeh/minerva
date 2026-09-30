# Minerva — Hermes install on the host machine (run in PowerShell as your normal user; no admin needed)
# Prepared 2026-06-09. Pauses before anything consequential; backs up before any overwrite.
# After this script: open the morning session and we verify the CONTRACT-TODOs together.

# Python writes text as UTF-8, so non-English characters can't crash it (added 2026-09-28).
$env:PYTHONUTF8 = "1"

$ErrorActionPreference = "Stop"
# HOST PATHS: env var first, then this script's own location, then the current folder.
# Same two variables the rest of the codebase uses (MINERVA_HOME, MINERVA_BRAIN).
$Project = if ($env:MINERVA_HOME) { $env:MINERVA_HOME }
           elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot }
           else { (Get-Location).Path }
$Brain   = if ($env:MINERVA_BRAIN) { $env:MINERVA_BRAIN } else { Join-Path $HOME "Brain" }
$HermesDir = Join-Path $env:LOCALAPPDATA "hermes"   # FIXED 2026-06-10: real Hermes home (live-verified); "~/.hermes" was a docs-sourced guess
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

Step "0/6  Protect existing work (read-only backups)"
if (Test-Path "$Brain\backups")   { attrib +R "$Brain\backups\*" /S }
if (Test-Path "$Project\backups") { attrib +R "$Project\backups\*" /S }
Write-Host "backups marked read-only"

Step "1/6  Install Hermes runtime (official installer: uv, Python 3.11, Node, MinGit)"
Write-Host "About to run the official Nous installer from hermes-agent.nousresearch.com"
Read-Host  "Press Enter to proceed (Ctrl+C to abort)"
Invoke-Expression (Invoke-RestMethod https://hermes-agent.nousresearch.com/install.ps1)

# Refresh PATH from registry so the tools that JUST installed (uv, MinGit, hermes)
# resolve in THIS session — otherwise steps 2-3 only work in a new window. (2026-06-10)
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

Step "2/6  Pin hermes-agent==0.16.0 (no LTS channel; we gate upgrades manually)"
# CONTRACT-TODO: confirm the pin mechanism the installer expects (uv tool vs pip).
$pinned = $false
try { uv tool install "hermes-agent==0.16.0" --force; $pinned = $true } catch {}
if (-not $pinned) { try { pip install "hermes-agent==0.16.0"; $pinned = $true } catch {} }
if (-not $pinned) { Write-Warning "Could not pin 0.16.0 automatically - note the installed version below; we will pin together in session." }
try { hermes --version } catch { Write-Warning "hermes not on PATH yet - open a NEW PowerShell window and re-run from step 2." }

Step "3/6  Git for the Minerva project (MinGit just arrived with Hermes)"
if (-not (Test-Path "$Project\.git")) {
  Set-Location $Project
  git init -b main
  git add -A
  git commit -m "minerva_core v0.1: ledger+governance+gateway+hermes adapter (pre-install snapshot)"
  Write-Host "git repo initialized + first commit"
} else { Write-Host "git repo already present - skipping" }

Step "4/6  Hermes config: memory OFF, Minerva gateway as sole MCP server"
if (Test-Path "$HermesDir\config.yaml") {
  Copy-Item "$HermesDir\config.yaml" "$HermesDir\config.yaml.$Stamp.bak"
  attrib +R "$HermesDir\config.yaml.$Stamp.bak"
  Write-Host "existing config backed up (read-only): config.yaml.$Stamp.bak"
}
New-Item -ItemType Directory -Force -Path $HermesDir | Out-Null
Copy-Item "$Project\config\hermes-config-minerva.yaml" "$HermesDir\config.yaml" -Force
New-Item -ItemType Directory -Force -Path "$Project\state" | Out-Null
Write-Host "config installed. NOTE: built-in toolsets still need disabling via 'hermes tools' (we do this together in session)."

Step "5/6  API server credentials"
# SUPERSEDED 2026-06-10 (live verification): Hermes 0.16.0 has NO API_SERVER_* env vars —
# that contract was a docs-guess. The HTTP surface is `hermes dashboard` (default
# 127.0.0.1:9119); auth mechanism per state/contract-report.json. NEVER overwrite the
# live .env here — it's Hermes's own template with provider/integration keys.
Write-Host "skipped (superseded - see comment; dashboard auth handled in configure_minerva.ps1 era)"

Step "6/6  Smoke-test the Minerva gateway itself (no Hermes needed for this)"
Set-Location $Project
# Find a Python without dying (a fresh machine may not have one on PATH yet). (2026-06-10)
$py = $null
foreach ($cand in @("python", "py")) {
  try { & $cand -c "import sys; assert sys.version_info >= (3,10)" 2>$null; if ($LASTEXITCODE -eq 0) { $py = $cand; break } } catch {}
}
if ($py) {
  & $py tests\test_ledger.py
  & $py tests\test_gateway.py
  & $py tests\test_adapter_hermes.py
} else {
  Write-Warning "No Python >=3.10 on PATH - skipping smoke tests; we'll sort Python in session (the gateway needs it)."
}

Write-Host "`nDONE. Next (in session together): start 'hermes gateway', disable built-in toolsets," -ForegroundColor Green
Write-Host "verify MCP handshake hits our gateway, then the CONTRACT-TODO field checks." -ForegroundColor Green
