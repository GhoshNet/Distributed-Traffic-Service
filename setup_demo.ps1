# =============================================================================
# setup_demo.ps1 — distributed demo setup (2–10 laptops, Windows PowerShell)
#
# Usage (run in PowerShell as Administrator):
#   .\setup_demo.ps1 -Label B -Ips 172.20.10.2,172.20.10.3,172.20.10.4,172.20.10.5
#
# Labels are assigned in order: first IP = A, second = B, etc.
# Pass ALL laptops' IPs in the same fixed order on every laptop.
#
# Examples:
#   2 laptops, you are A:  .\setup_demo.ps1 -Label A -Ips 192.168.1.10,192.168.1.11
#   4 laptops, you are B:  .\setup_demo.ps1 -Label B -Ips 172.20.10.2,172.20.10.3,172.20.10.4,172.20.10.5
# =============================================================================

param(
    [Parameter(Mandatory=$true)][string]$Label,
    [Parameter(Mandatory=$true)][string]$Ips
)

$ErrorActionPreference = "Stop"

function Info($msg)    { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Success($msg) { Write-Host "[OK] $msg"   -ForegroundColor Green }
function Warn($msg)    { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg)     { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

$AllLabels = @('A','B','C','D','E','F','G','H','I','J')
$Label = $Label.ToUpper()
$IpList = $Ips -split ','
$N = $IpList.Count

if ($N -lt 2) { Err "Need at least 2 IPs. Got: $N" }

$MyIndex = $AllLabels.IndexOf($Label)
if ($MyIndex -eq -1) { Err "Label must be A-J, got '$Label'" }
if ($MyIndex -ge $N) { Err "Label '$Label' is position $($MyIndex+1) but only $N IPs provided" }

$MyIp = $IpList[$MyIndex]

# Build peer lists
$PeerConflictList = @(); $PeerUserList = @()
for ($i = 0; $i -lt $N; $i++) {
    if ($i -ne $MyIndex) {
        $PeerConflictList += "http://$($IpList[$i]):8003"
        $PeerUserList     += "http://$($IpList[$i]):8080"
    }
}
$PeerConflictUrls = $PeerConflictList -join ","
$PeerUserUrls     = $PeerUserList -join ","

Write-Host ""
Write-Host "=============================================" -ForegroundColor White
Write-Host " Distributed Traffic System — Demo Setup"    -ForegroundColor White
Write-Host " Laptop $Label  |  My IP: $MyIp  |  Nodes: $N" -ForegroundColor White
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
$envContent = "PEER_CONFLICT_URLS=$PeerConflictUrls`nPEER_USER_URLS=$PeerUserUrls`nMY_LABEL=$Label`nNODE_COUNT=$N`n"
for ($i = 0; $i -lt $N; $i++) {
    $envContent += "IP_$($AllLabels[$i])=$($IpList[$i])`n"
}
$envContent | Set-Content .env -Encoding UTF8
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
bash deploy-swarm.sh

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
for ($i = 0; $i -lt $N; $i++) {
    if ($i -ne $MyIndex) {
        $lbl = $AllLabels[$i]; $ip = $IpList[$i]
        try {
            $body = '{"name": "laptop-' + $lbl + '", "health_url": "http://' + $ip + ':8080/health"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8080/admin/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,201) { Success "Registered peer laptop-$lbl ($ip)" }
        } catch { Warn "Could not register laptop-$lbl yet — run .\register_peers.ps1 after all nodes are up" }
    }
}

# Step 9: Conflict catch-up sync
Write-Host ""
Info "Triggering conflict-service catch-up sync..."
Start-Sleep 2
for ($i = 0; $i -lt $N; $i++) {
    if ($i -ne $MyIndex) {
        $lbl = $AllLabels[$i]; $ip = $IpList[$i]
        try {
            $body = '{"peer_url": "http://' + $ip + ':8003"}'
            $r = Invoke-WebRequest -Uri "http://localhost:8003/internal/peers/register" `
                -Method POST -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -in 200,204) { Success "Conflict sync registered with laptop-$lbl ($ip)" }
        } catch { Warn "Conflict sync to laptop-$lbl not ready yet" }
    }
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host " Setup complete — Laptop $Label ($N nodes)"   -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Frontend:    http://localhost:3000"
Write-Host "  API Gateway: http://localhost:8080"
Write-Host "  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)"
Write-Host ""
Write-Host "  All nodes:"
for ($i = 0; $i -lt $N; $i++) {
    $lbl = $AllLabels[$i]; $ip = $IpList[$i]
    if ($i -eq $MyIndex) { Write-Host "    Laptop $lbl`: $ip  <- YOU" -ForegroundColor Green }
    else                  { Write-Host "    Laptop $lbl`: $ip" }
}
Write-Host ""
Write-Host "  Kill this node:"
Write-Host "    curl -X POST http://localhost:8080/admin/simulate/fail"
Write-Host "  Recover:"
Write-Host "    curl -X POST http://localhost:8080/admin/simulate/recover"
Write-Host ""
Write-Host "  If peer registration failed, run: .\register_peers.ps1"
Write-Host ""
