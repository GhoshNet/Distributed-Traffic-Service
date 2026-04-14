# =============================================================================
# setup_demo.ps1 — 4-laptop distributed demo setup (Windows PowerShell)
#
# Usage (run in PowerShell as Administrator):
#   .\setup_demo.ps1 -Label B -IpA 172.20.10.2 -IpB 172.20.10.3 -IpC 172.20.10.4 -IpD 172.20.10.5
#
# Labels: A, B, C, D  — tells the script which IP is yours
# IPs: pass all 4 in order A B C D, same order on every laptop
# =============================================================================

param(
    [Parameter(Mandatory=$true)][ValidateSet("A","B","C","D")][string]$Label,
    [Parameter(Mandatory=$true)][string]$IpA,
    [Parameter(Mandatory=$true)][string]$IpB,
    [Parameter(Mandatory=$true)][string]$IpC,
    [Parameter(Mandatory=$true)][string]$IpD
)

$ErrorActionPreference = "Stop"

function Info($msg)    { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Success($msg) { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg)    { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg)     { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# Map label to own IP
$IpMap = @{ A=$IpA; B=$IpB; C=$IpC; D=$IpD }
$MyIp = $IpMap[$Label]

# Build peer lists (all except self)
$PeerConflictUrls = ($IpMap.GetEnumerator() | Where-Object { $_.Key -ne $Label } | ForEach-Object { "http://$($_.Value):8003" }) -join ","
$PeerUserUrls     = ($IpMap.GetEnumerator() | Where-Object { $_.Key -ne $Label } | ForEach-Object { "http://$($_.Value):8080" }) -join ","

Write-Host ""
Write-Host "=============================================" -ForegroundColor White
Write-Host " Distributed Traffic System — Demo Setup"    -ForegroundColor White
Write-Host " Laptop $Label  |  My IP: $MyIp"             -ForegroundColor White
Write-Host "=============================================" -ForegroundColor White
Write-Host ""
Info "Peer conflict URLs : $PeerConflictUrls"
Info "Peer user URLs     : $PeerUserUrls"
Write-Host ""

# Step 1: Verify IP
$ActualIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -notmatch "Loopback" -and
    $_.InterfaceAlias -notmatch "Virtual" -and
    $_.IPAddress -notmatch "^169\." -and
    $_.IPAddress -ne "127.0.0.1"
} | Select-Object -First 1).IPAddress

if ($ActualIp -ne $MyIp) {
    Warn "Detected IP $ActualIp but you said you are $MyIp"
    Warn "Make sure you are connected to the hotspot."
    $confirm = Read-Host "Continue anyway? [y/N]"
    if ($confirm -notmatch "^[Yy]$") { exit 1 }
} else {
    Success "IP confirmed: $ActualIp"
}

# Step 2: Check Docker Swarm
Info "Checking Docker Swarm..."
$swarmState = (docker info 2>$null | Select-String "Swarm:").ToString().Trim()
if ($swarmState -notmatch "active") {
    Info "Swarm not active — initialising..."
    docker swarm init --advertise-addr $MyIp
    Success "Swarm initialised"
} else {
    Success "Swarm already active"
}

# Step 3: Write .env
Info "Writing .env..."
@"
PEER_CONFLICT_URLS=$PeerConflictUrls
PEER_USER_URLS=$PeerUserUrls
MY_LABEL=$Label
IP_A=$IpA
IP_B=$IpB
IP_C=$IpC
IP_D=$IpD
"@ | Set-Content .env -Encoding UTF8
Success ".env written"

# Step 4: Local registry
Info "Ensuring local Docker registry is running..."
$registryRunning = docker service ls 2>$null | Select-String "registry"
if (-not $registryRunning) {
    docker service create --name registry --publish published=5000,target=5000 registry:2
    Start-Sleep 6
    Success "Registry started"
} else {
    Success "Registry already running"
}

# Step 5: Export env vars and deploy
$env:PEER_CONFLICT_URLS = $PeerConflictUrls
$env:PEER_USER_URLS     = $PeerUserUrls

Info "Building images and deploying stack..."
.\deploy-swarm.sh   # Git Bash / WSL will handle this
# If the above fails, try: bash deploy-swarm.sh

# Step 6: Wait for services
Write-Host ""
Info "Waiting for services (up to 3 minutes)..."
for ($i = 1; $i -le 36; $i++) {
    Start-Sleep 5
    $lines = docker service ls 2>$null | Select-String "traffic-service"
    $total = ($lines | Measure-Object).Count
    $ready = ($lines | Where-Object { $_ -match "\s(\d+)/\1\s" } | Measure-Object).Count
    Write-Host -NoNewline "`r  Services ready: $ready / $total   ($($i*5)s elapsed)"
    if ($ready -eq $total -and $total -gt 0) { Write-Host ""; break }
}
Write-Host ""

# Step 7: Health check
Info "Checking gateway health..."
Start-Sleep 3
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:8080/health" -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -eq 200) { Success "Gateway healthy (HTTP 200)" }
} catch {
    Warn "Gateway not responding yet — try: curl http://localhost:8080/health"
}

# Step 8: Register peers
Write-Host ""
Info "Registering peer health monitors..."
Start-Sleep 5
foreach ($key in $IpMap.Keys) {
    if ($key -ne $Label) {
        $ip = $IpMap[$key]
        try {
            $body = '{"name": "laptop-' + $key + '", "health_url": "http://' + $ip + ':8080/health"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8080/admin/peers/register" `
                -Method POST -Body $body -ContentType "application/json" `
                -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,201) { Success "Registered peer laptop-$key ($ip)" }
        } catch { Warn "Could not register laptop-$key yet — run .\register_peers.ps1 after all nodes are up" }
    }
}

# Step 9: Conflict catch-up sync
Write-Host ""
Info "Triggering conflict-service catch-up sync..."
Start-Sleep 2
foreach ($key in $IpMap.Keys) {
    if ($key -ne $Label) {
        $ip = $IpMap[$key]
        try {
            $body = '{"peer_url": "http://' + $ip + ':8003"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8003/internal/peers/register" `
                -Method POST -Body $body -ContentType "application/json" `
                -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,204) { Success "Conflict sync registered with laptop-$key ($ip)" }
        } catch { Warn "Conflict sync to laptop-$key not ready yet" }
    }
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host " Setup complete — Laptop $Label"              -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Frontend:    http://localhost:3000"
Write-Host "  API Gateway: http://localhost:8080"
Write-Host "  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)"
Write-Host ""
Write-Host "  Peer IPs:"
foreach ($key in @("A","B","C","D")) {
    $ip = $IpMap[$key]
    if ($key -eq $Label) { Write-Host "    Laptop $key`: $ip  <- YOU" -ForegroundColor Green }
    else                  { Write-Host "    Laptop $key`: $ip" }
}
Write-Host ""
Write-Host "  Kill this node:"
Write-Host "    curl -X POST http://localhost:8080/admin/simulate/fail"
Write-Host "  Recover:"
Write-Host "    curl -X POST http://localhost:8080/admin/simulate/recover"
Write-Host ""
Write-Host "  If peer registration failed, run: .\register_peers.ps1"
Write-Host ""
