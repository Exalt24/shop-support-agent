# One command to bring the support console up for a call, locally, with clean FAKE data.
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_console_demo.ps1            # console on http://localhost:3100
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_console_demo.ps1 -Tunnel    # also a public ngrok link
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_console_demo.ps1 -Stop      # stop what this started
# Demo logins are local only: admin@demo.test / demo-admin-pass. The seed wipes and refills the LOCAL database, never point it at real data.
param([switch]$Tunnel, [switch]$Stop)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here ".venv\Scripts\python.exe"

function Stop-Port($port) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}

if ($Stop) {
    Stop-Port 3100; Stop-Port 8010
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -match "ngrok_tunnel" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    docker stop support-console-pg | Out-Null
    Write-Host "stopped"; exit 0
}

docker start support-console-pg | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    if ((docker exec support-console-pg pg_isready -U postgres 2>$null) -match "accepting") { break }
    Start-Sleep -Seconds 1
}

Push-Location $here
& $py -m console_api.migrate --reset | Out-Host
& $py -m console_api.seed | Out-Host

Stop-Port 8010; Stop-Port 3100
$env:CONSOLE_ALLOWED_ORIGINS = "http://localhost:3100"
Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList "-m", "uvicorn", "console_api.main:app", "--port", "8010" `
    -RedirectStandardOutput (Join-Path $here "logs\console_api.out.log") -RedirectStandardError (Join-Path $here "logs\console_api.err.log")

Push-Location (Join-Path $here "console_web")
if (-not (Test-Path ".next\BUILD_ID")) { npm run build | Out-Host }
Start-Process -WindowStyle Hidden -FilePath "cmd.exe" -ArgumentList "/c", "npx next start -p 3100" `
    -RedirectStandardOutput (Join-Path $here "logs\console_web.out.log") -RedirectStandardError (Join-Path $here "logs\console_web.err.log")
Pop-Location; Pop-Location

for ($i = 0; $i -lt 60; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:3100/login" -TimeoutSec 3).StatusCode -eq 200) { break } } catch { Start-Sleep -Seconds 1 }
}
Write-Host "console: http://localhost:3100  (login admin@demo.test / demo-admin-pass)"

if ($Tunnel) {
    $root = Resolve-Path (Join-Path $here "..\..\..")
    Start-Process -WindowStyle Hidden -FilePath "node" -ArgumentList (Join-Path $root "Operations\tools\ngrok_tunnel\tunnel.cjs"), "3100" `
        -RedirectStandardOutput (Join-Path $here "logs\tunnel.out.log") -RedirectStandardError (Join-Path $here "logs\tunnel.err.log")
    $tlog = Join-Path $here "logs\tunnel.out.log"
    for ($i = 0; $i -lt 40; $i++) {
        if ((Test-Path $tlog) -and ((Get-Content $tlog -Raw) -match "https://\S+")) { break }
        Start-Sleep -Milliseconds 500
    }
    Get-Content $tlog | Select-Object -Last 3
}
