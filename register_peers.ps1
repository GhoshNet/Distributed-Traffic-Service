# register_peers.ps1 — Re-register all peers without redeploying (Windows)
# Run if setup_demo.ps1 peer registration failed.
# Reads .env written by setup_demo.ps1

function Success($msg) { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg)    { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Info($msg)    { Write-Host "[INFO] $msg" -ForegroundColor Cyan }

if (-not (Test-Path .env)) { Write-Host "Run setup_demo.ps1 first — .env not found"; exit 1 }

# Parse .env
$env_vars = @{}
Get-Content .env | ForEach-Object {
    if ($_ -match "^([^=]+)=(.*)$") { $env_vars[$matches[1]] = $matches[2] }
}
$MyLabel = $env_vars["MY_LABEL"]
$IpMap = @{ A=$env_vars["IP_A"]; B=$env_vars["IP_B"]; C=$env_vars["IP_C"]; D=$env_vars["IP_D"] }

Write-Host ""
Info "Registering peers for Laptop $MyLabel..."
Write-Host ""

foreach ($key in $IpMap.Keys) {
    if ($key -ne $MyLabel) {
        $ip = $IpMap[$key]

        # Health peer
        try {
            $body = '{"name": "laptop-' + $key + '", "health_url": "http://' + $ip + ':8080/health"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8080/admin/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,201) { Success "Health peer laptop-$key ($ip) registered" }
        } catch { Warn "Health peer laptop-$key`: not reachable (HTTP error)" }

        # Conflict sync
        try {
            $body = '{"peer_url": "http://' + $ip + ':8003"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8003/internal/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,204) { Success "Conflict peer laptop-$key ($ip:8003) + catch-up triggered" }
        } catch { Warn "Conflict peer laptop-$key`: not reachable" }
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
