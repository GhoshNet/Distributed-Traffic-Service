# register_peers.ps1 — Re-register all peers without redeploying (Windows)
# Run if setup_demo.ps1 peer registration failed. Reads .env written by setup_demo.ps1.

function Success($msg) { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg)    { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Info($msg)    { Write-Host "[INFO] $msg" -ForegroundColor Cyan }

if (-not (Test-Path .env)) { Write-Host "Run setup_demo.ps1 first — .env not found"; exit 1 }

# Parse .env
$env_vars = @{}
Get-Content .env | ForEach-Object {
    if ($_ -match "^([^=]+)=(.*)$") { $env_vars[$matches[1]] = $matches[2] }
}

$MyLabel   = $env_vars["MY_LABEL"]
$NodeCount = [int]$env_vars["NODE_COUNT"]
$AllLabels = @('A','B','C','D','E','F','G','H','I','J')

# Rebuild IP list from .env
$IpList = @()
for ($i = 0; $i -lt $NodeCount; $i++) {
    $IpList += $env_vars["IP_$($AllLabels[$i])"]
}

$MyIndex = $AllLabels.IndexOf($MyLabel)

Write-Host ""
Info "Registering peers for Laptop $MyLabel ($NodeCount nodes total)..."
Write-Host ""

for ($i = 0; $i -lt $NodeCount; $i++) {
    if ($i -ne $MyIndex) {
        $lbl = $AllLabels[$i]; $ip = $IpList[$i]

        try {
            $body = '{"name": "laptop-' + $lbl + '", "health_url": "http://' + $ip + ':8080/health"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8080/admin/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,201) { Success "Health peer laptop-$lbl ($ip) registered" }
        } catch { Warn "Health peer laptop-$lbl`: not reachable" }

        try {
            $body = '{"peer_url": "http://' + $ip + ':8003"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8003/internal/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,204) { Success "Conflict peer laptop-$lbl ($ip:8003) registered + catch-up triggered" }
        } catch { Warn "Conflict peer laptop-$lbl`: not reachable" }

        try {
            $body = '{"peer_url": "http://' + $ip + ':8080"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8080/internal/journeys/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,201) { Success "Journey peer laptop-$lbl ($ip:8080) registered + catch-up triggered" }
        } catch { Warn "Journey peer laptop-$lbl`: not reachable" }
    }
}

Write-Host ""
Info "Peer health status:"
try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/health/nodes" -UseBasicParsing -TimeoutSec 5
    Write-Host $r.Content
} catch { Warn "Could not reach /health/nodes" }

Write-Host ""
Success "Done. Open http://localhost:3000 → Simulate tab → peer cards should show ALIVE within 10s."
