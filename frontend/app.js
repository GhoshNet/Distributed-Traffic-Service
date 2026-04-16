// Auto-detect host so the frontend works from any device on the network
const API = window.location.protocol + '//' + window.location.hostname + ':8080';
const WS  = (window.location.protocol === 'https:' ? 'wss://' : 'ws://') + window.location.hostname + ':8080';

let token = localStorage.getItem('jb_token');
let user = JSON.parse(localStorage.getItem('jb_user') || 'null');
let wsConn = null;
let mapInstance = null;
let markers = {};
let layerGroup = null;
let toastCounter = 0;

// Failover state — peer API bases discovered from /health/nodes.
// Persisted in localStorage so peers are known even on the login screen
// before the user has authenticated.
let activePeerAPIs = JSON.parse(localStorage.getItem('jb_peers') || '[]');
let currentAPIBase = API;  // tracks which node is currently serving requests

// Geocoding state — stores the selected location objects
let selectedOrigin = null;
let selectedDest = null;
let geocodeTimers = {};

// Road network state (predefined Irish routes from conflict-service)
let predefinedRoutes = [];
let roadNetworkLayer = null;
let roadNetworkVisible = true;

// Map mode: 'routes' = show all predefined routes, 'journeys' = show user's journeys only
let mapMode = 'routes';

// Route colour palette (one per predefined route)
const ROUTE_COLORS = ['#00b4d8', '#f77f00', '#06d6a0', '#e63946', '#8338ec', '#ffbe0b'];

// Routing logic — validate token on refresh before entering app
if (token && user) {
  authFetch('/api/users/me').then(r => {
    if (r.ok) {
      r.json().then(u => { user = u; localStorage.setItem('jb_user', JSON.stringify(u)); });
      enterApp();
    } else {
      localStorage.clear();
      document.getElementById('auth-screen').style.display = 'flex';
    }
  }).catch(() => {
    // Network down — trust cached token and try anyway
    enterApp();
  });
} else {
  document.getElementById('auth-screen').style.display = 'flex';
}

function switchAuth(tab) {
  document.getElementById('login-form').style.display = tab === 'login' ? 'block' : 'none';
  document.getElementById('register-form').style.display = tab === 'register' ? 'block' : 'none';
  document.querySelectorAll('.auth-tab').forEach((el, i) => el.classList.toggle('active', (tab==='login')===(i===0)));
}

async function login(e) {
  e.preventDefault();
  try {
    const r = await resilientFetch('/api/users/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: e.target[0].value, password: e.target[1].value})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(parseErrorDetail(d));
    token = d.access_token; localStorage.setItem('jb_token', token);
    const p = await resilientFetch('/api/users/me', {headers:{'Authorization': `Bearer ${token}`}});
    user = await p.json(); localStorage.setItem('jb_user', JSON.stringify(user));
    enterApp();
  } catch(err) { showToast("Login failed: " + err.message, "error"); }
}

// =============================================
// BUG FIX 1: Parse error detail properly
// Handles both string detail (409/401) and array detail (422 validation errors)
// =============================================
function parseErrorDetail(data) {
    if (!data) return 'Unknown error';
    const detail = data.detail;
    if (!detail) return data.message || JSON.stringify(data);
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(err => {
            const field = err.loc ? err.loc[err.loc.length - 1] : 'field';
            return `${field}: ${err.msg}`;
        }).join('; ');
    }
    return String(detail);
}

async function register(e) {
    e.preventDefault();
    try {
        const r = await resilientFetch('/api/users/register', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                full_name: e.target[0].value, email: e.target[1].value,
                license_number: e.target[2].value, password: e.target[3].value
            })
        });
        if(!r.ok) throw new Error(parseErrorDetail(await r.json()));
        switchAuth('login');
        showToast("Registered successfully. Please login.", "success");
    } catch(err) { showToast(err.message, "error"); }
}

function logout() {
  localStorage.clear(); location.reload();
}

async function enterApp() {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  document.getElementById('user-name').innerText = user.full_name;
  initMap();
  go('map');
  connectWS();
  setupAutocomplete();
  await Promise.all([loadVehicles(), loadPredefinedRoutes(), loadPeerAPIs()]);
  // Set default departure time
  let d = new Date(); d.setHours(d.getHours()+1);
  document.getElementById('j-depart').value = d.toISOString().slice(0,16);
}

function go(view) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(`view-${view}`).classList.add('active');
  document.getElementById(`nav-${view}`).classList.add('active');

  // Stop health polling when leaving simulate
  if (view !== 'simulate') stopNodeHealthPolling();

  if(view === 'map') {
    setTimeout(() => mapInstance.invalidateSize(), 300);
    loadJourneys();
  }
  if(view === 'journeys') { loadJourneys(); loadVehicles(); if(predefinedRoutes.length === 0) loadPredefinedRoutes(); }
  if(view === 'simulate') { loadDashboard(); startNodeHealthPolling(); }
  if(view === 'enforcement') { document.getElementById('enf-result').style.display = 'none'; }
}

function initMap() {
  mapInstance = L.map('map').setView([53.1424, -7.6921], 7);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(mapInstance);
  layerGroup = L.layerGroup().addTo(mapInstance);
}

let _wsFailCount = 0;  // consecutive WS close/error count — used to trigger peer failover

function connectWS(baseOverride) {
  // Derive WS base: prefer override, then currentAPIBase, then API
  const httpBase = baseOverride || currentAPIBase || API;
  const wsBase = httpBase.replace(/^http/, 'ws');
  const url = `${wsBase}/ws/notifications/?token=${token}`;

  const dot = document.getElementById('ws-dot');
  wsConn = new WebSocket(url);

  wsConn.onopen = () => {
    dot.className = 'ws-dot connected';
    _wsFailCount = 0;
    console.info('[ws] connected to', url);
  };

  wsConn.onclose = (e) => {
    dot.className = 'ws-dot';
    _wsFailCount++;
    // Code 4001 = auth rejected — token is bad, force re-login
    if (e.code === 4001 || e.code === 4003) {
      console.warn('[ws] auth rejected by server — clearing session');
      localStorage.clear(); location.reload(); return;
    }
    // After 2 consecutive failures on the current base, try the next ALIVE peer WS.
    // This ensures live notifications survive a primary node crash.
    const allBases = [API, ...activePeerAPIs.filter(p => p !== API)];
    const nextBase = (_wsFailCount >= 2 && allBases.length > 1)
        ? allBases[_wsFailCount % allBases.length]
        : null;
    if (nextBase && nextBase !== httpBase) {
        console.info(`[ws] primary unreachable after ${_wsFailCount} attempts — failing over to ${nextBase}`);
    }
    setTimeout(() => connectWS(nextBase || httpBase), Math.min(5000 * _wsFailCount, 30000));
  };

  wsConn.onmessage = e => {
    if(e.data === 'pong') return;
    try {
      const data = JSON.parse(e.data);
      handleLiveEvent(data);
    } catch {}
  };

  setInterval(() => wsConn.readyState === 1 && wsConn.send('ping'), 25000);
}

function handleLiveEvent(data) {
    // Show toast
    const toast = document.createElement('div');
    toast.className = 'event-toast';
    const ts = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : '';
    toast.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
            <strong style="font-size:14px">${data.title}</strong>
            ${ts ? `<span style="font-size:10px;opacity:0.6;white-space:nowrap">${ts}</span>` : ''}
        </div>
        <div style="font-size:12px;opacity:0.85;margin-top:5px;line-height:1.4">${data.message}</div>`;
    document.body.appendChild(toast);
    setTimeout(()=> toast.remove(), 9000);

    // If journey confirmed, add a line to map
    if(data.event_type === "journey.confirmed" && data.metadata) {
        const m = data.metadata;
        if(m.origin_lat) {
            const org = [m.origin_lat, m.origin_lng];
            const dst = [m.destination_lat, m.destination_lng];
            L.polyline([org, dst], {color: '#8a2be2', weight: 3, opacity: 0.7, dashArray: '5,10'}).addTo(layerGroup);
            L.circleMarker(org, {radius:6, color:'#00e676', fillOpacity:1}).bindPopup(`Origin: ${m.origin}`).addTo(layerGroup);
            L.circleMarker(dst, {radius:6, color:'#ff1744', fillOpacity:1}).bindPopup(`Dest: ${m.destination}`).addTo(layerGroup);
        }
    }

    // Refresh journey list when we get journey events via WebSocket
    if(data.event_type && data.event_type.startsWith("journey.")) {
        loadJourneys();
    }
}

// loadPeerAPIs — reads registered health peers and extracts their API base URLs.
// Tries primary node first; if it is down, tries each already-known peer so the
// list stays fresh even when the primary node is fully offline.
async function loadPeerAPIs() {
    // Build the list of bases to try: primary first, then cached peers
    const cached = JSON.parse(localStorage.getItem('jb_peers') || '[]');
    const bases = [API, ...cached.filter(p => p !== API)];

    for (const base of bases) {
        try {
            const r = await fetch(base + '/health/nodes', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (!r.ok) continue;
            const data = await r.json();
            // laptop_peers = dynamically-registered remote machines
            // data.peers = internal microservices — NOT useful as API failover bases
            const fresh = Object.values(data.laptop_peers || {})
                .filter(p => p.status === 'ALIVE' && p.ping_url)
                .map(p => p.ping_url.replace(/\/health.*$/, ''));
            // Merge: keep fresh peers, cached peers, and exclude self
            const merged = [...new Set([...fresh, ...cached])].filter(p => p && p !== API);
            activePeerAPIs = merged;
            localStorage.setItem('jb_peers', JSON.stringify(activePeerAPIs));
            
            if (activePeerAPIs.length > 0) {
                console.info('[failover] discovered cluster peers:', activePeerAPIs);
                updateFailoverIndicator();
            }
            return;
        } catch(e) {
            // If primary is down, we don't give up — we keep trying other cached bases
            console.warn(`[failover] discovery attempt failed at ${base}:`, e.message);
        }
    }
}

// updateFailoverIndicator — shows how many peers are in the failover cache
function updateFailoverIndicator() {
    const el = document.getElementById('peer-count-tag');
    if (!el) return;
    const count = activePeerAPIs.length;
    el.innerHTML = count > 0 ? `🔗 Cluster: ${count} peers` : '📍 Standalone';
    el.style.opacity = count > 0 ? '1' : '0.5';
}

// setActiveNode — updates the topbar indicator when failover switches nodes.
function setActiveNode(base) {
    currentAPIBase = base;
    const el = document.getElementById('node-indicator');
    if (!el) return;
    if (base === API) {
        el.innerHTML = '🟢 Primary';
        el.style.color = 'var(--success)';
    } else {
        const label = base.replace(/^https?:\/\//, '');
        el.innerHTML = `⚡ Failover: ${label}`;
        el.style.color = 'var(--warning)';
        showToast(`Node failover — now routing to ${label}`, 'warning');
    }
}

// resilientFetch — base failover fetch. No auth header.
// Tries primary node first; on network error or 5xx retries each ALIVE peer.
// 4xx responses are returned immediately (app errors, not node failures).
// Used directly by login, register, and any pre-auth calls.
async function resilientFetch(url, opts={}) {
    const allBases = [API, ...activePeerAPIs.filter(p => p !== API)];

    for (let i = 0; i < allBases.length; i++) {
        const base = allBases[i];
        try {
            const resp = await fetch(base + url, { ...opts });
            if (resp.ok || (resp.status >= 400 && resp.status < 500)) {
                if (base !== currentAPIBase) setActiveNode(base);
                return resp;
            }
            console.warn(`[failover] ${base} returned HTTP ${resp.status}`);
        } catch(e) {
            console.warn(`[failover] ${base} unreachable: ${e.message}`);
        }
        if (i < allBases.length - 1) {
            console.info(`[failover] trying next node: ${allBases[i+1]}`);
        }
    }
    throw new Error('All nodes unreachable — check your connection');
}

// authFetch — resilientFetch with the JWT bearer token attached.
// Every authenticated API call in the app goes through here.
async function authFetch(url, opts={}) {
    opts.headers = opts.headers || {};
    opts.headers['Authorization'] = `Bearer ${token}`;
    return resilientFetch(url, opts);
}

// =============================================
// BUG FIX 3: Geocoding autocomplete (replaces hardcoded cities)
// Uses OpenStreetMap Nominatim for free worldwide geocoding
// =============================================
function setupAutocomplete() {
    setupGeoInput('j-origin', 'j-origin-results', (place) => {
        selectedOrigin = place;
    });
    setupGeoInput('j-dest', 'j-dest-results', (place) => {
        selectedDest = place;
    });
}

function setupGeoInput(inputId, resultsId, onSelect) {
    const input = document.getElementById(inputId);
    const results = document.getElementById(resultsId);

    input.addEventListener('input', () => {
        const query = input.value.trim();
        if (query.length < 3) { results.innerHTML = ''; results.style.display = 'none'; return; }

        // Debounce: wait 350ms after last keystroke
        clearTimeout(geocodeTimers[inputId]);
        geocodeTimers[inputId] = setTimeout(() => geocodeSearch(query, results, input, onSelect), 350);
    });

    input.addEventListener('blur', () => {
        // Delay hide so click events on results fire first
        setTimeout(() => { results.style.display = 'none'; }, 200);
    });

    input.addEventListener('focus', () => {
        if (results.innerHTML) results.style.display = 'block';
    });
}

async function geocodeSearch(query, resultsEl, inputEl, onSelect) {
    try {
        const url = `https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(query)}&limit=6&addressdetails=1`;
        const r = await fetch(url, { headers: { 'Accept-Language': 'en' } });
        const places = await r.json();

        if (places.length === 0) {
            resultsEl.innerHTML = '<div class="autocomplete-item no-results">No results found</div>';
            resultsEl.style.display = 'block';
            return;
        }

        resultsEl.innerHTML = places.map((p, i) => `
            <div class="autocomplete-item" data-idx="${i}">
                <div class="ac-name">${p.display_name.split(',').slice(0,3).join(', ')}</div>
                <div class="ac-detail">${p.display_name}</div>
            </div>
        `).join('');
        resultsEl.style.display = 'block';

        // Attach click handlers
        resultsEl.querySelectorAll('.autocomplete-item:not(.no-results)').forEach((el) => {
            el.addEventListener('mousedown', (e) => {
                e.preventDefault();
                const idx = parseInt(el.dataset.idx);
                const place = places[idx];
                inputEl.value = place.display_name.split(',').slice(0,3).join(', ');
                onSelect({
                    name: place.display_name.split(',').slice(0,3).join(', '),
                    full_name: place.display_name,
                    lat: parseFloat(place.lat),
                    lng: parseFloat(place.lon),
                });
                resultsEl.style.display = 'none';
            });
        });
    } catch(err) {
        console.error('Geocoding error:', err);
        resultsEl.innerHTML = '<div class="autocomplete-item no-results">Search failed</div>';
        resultsEl.style.display = 'block';
    }
}

async function bookJourney(e) {
    e.preventDefault();
    if(!selectedOrigin) return showToast("Please search and select an origin location", "error");
    if(!selectedDest) return showToast("Please search and select a destination location", "error");

    const vehicleSelect = document.getElementById('j-vehicle');
    const selectedVehicle = vehicleSelect.value;
    if(!selectedVehicle) return showToast("Please select a registered vehicle", "error");

    // Parse "REGISTRATION|TYPE" from the select value
    const [plate, vtype] = selectedVehicle.split('|');

    // Attach route_id if a quick-route was selected
    const quickRouteId = document.getElementById('j-quick-route').value || undefined;
    const protocol = document.getElementById('j-protocol').value || 'saga';
    const bookingUrl = protocol === '2pc' ? '/api/journeys/?mode=2pc' : '/api/journeys/';

    const payload = {
        origin: selectedOrigin.name, destination: selectedDest.name,
        origin_lat: selectedOrigin.lat, origin_lng: selectedOrigin.lng,
        destination_lat: selectedDest.lat, destination_lng: selectedDest.lng,
        departure_time: new Date(document.getElementById('j-depart').value).toISOString(),
        estimated_duration_minutes: parseInt(document.getElementById('j-dur').value),
        vehicle_registration: plate,
        vehicle_type: vtype,
        ...(quickRouteId ? { route_id: quickRouteId } : {}),
    };

    try {
        const r = await authFetch(bookingUrl, {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if(!r.ok) throw new Error(parseErrorDetail(d));
        if(d.status === "REJECTED") {
            showToast(`Rejected: ${d.rejection_reason}`, "error");
        } else {
            showToast("Journey booked successfully!", "success");
        }
        // Render the newly created journey immediately from the POST response
        appendJourneyToList(d);
        // Then refresh the full list from the server
        await loadJourneys();
    } catch(err) { showToast(err.message, "error"); }
}

function renderJourneyItem(j) {
    const canCancel = j.status === 'CONFIRMED' || j.status === 'PENDING';
    return `<div class="data-item">
        <div>
            <div style="font-weight:600;font-size:15px;margin-bottom:4px">${j.origin} → ${j.destination}</div>
            <div style="font-size:12px;color:var(--text-muted)">${new Date(j.departure_time).toLocaleString()} | ${j.vehicle_registration} (${j.vehicle_type})</div>
            ${j.rejection_reason ? `<div style="font-size:12px;color:var(--warning);margin-top:4px">Reason: ${j.rejection_reason}</div>` : ''}
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
            <span class="badge badge-${j.status.toLowerCase()}">${j.status}</span>
            ${canCancel ? `<button class="btn btn-sm" style="background:var(--danger);color:#fff;padding:2px 8px;font-size:11px" onclick="cancelJourney('${j.id}')">Cancel</button>` : ''}
        </div>
    </div>`;
}

async function cancelJourney(journeyId) {
    if (!confirm('Cancel this journey?')) return;
    try {
        const r = await authFetch(`/api/journeys/${journeyId}`, { method: 'DELETE' });
        const d = await r.json();
        if (!r.ok) throw new Error(parseErrorDetail(d));
        showToast(`Journey cancelled`, 'success');
        await loadJourneys();
    } catch(err) { showToast(err.message, 'error'); }
}

function appendJourneyToList(j) {
    const list = document.getElementById('journey-list');
    list.insertAdjacentHTML('afterbegin', renderJourneyItem(j));
}

async function loadJourneys() {
    try {
        const r = await authFetch('/api/journeys/');
        if(!r.ok) {
            console.error('Failed to load journeys:', r.status, r.statusText);
            const list = document.getElementById('journey-list');
            if (list) list.innerHTML = '<div style="color:var(--danger);text-align:center;padding:24px;">Could not load journeys (server error). Please refresh.</div>';
            return;
        }
        const js = await r.json();
        const journeys = js.journeys || js || [];
        const list = document.getElementById('journey-list');

        if(journeys.length === 0) {
            list.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:24px;">No journeys yet. Book one!</div>';
        } else {
            list.innerHTML = journeys.map(j => renderJourneyItem(j)).join('');
        }

        // Update map only when in 'journeys' mode
        try {
            if (mapMode === 'journeys') {
                layerGroup.clearLayers();
                const statusColors = { CONFIRMED: '#06d6a0', IN_PROGRESS: '#00b4d8', PENDING: '#ffbe0b', CANCELLED: '#888', REJECTED: '#e63946' };
                let journeyLegendHtml = '';
                journeys.forEach(j => {
                    if (j.origin_lat) {
                        const color = statusColors[j.status] || '#888';
                        const org = [j.origin_lat, j.origin_lng];
                        const dst = [j.destination_lat, j.destination_lng];
                        const dep = j.departure_time ? new Date(j.departure_time).toLocaleString() : '';
                        L.polyline([org, dst], { color, weight: 3, opacity: 0.8, dashArray: j.status === 'CONFIRMED' ? null : '6,8' })
                            .bindPopup(`<strong>${j.origin} → ${j.destination}</strong><br>${dep}<br><span style="color:${color}">${j.status}</span>`)
                            .addTo(layerGroup);
                        L.circleMarker(org, { radius: 6, color, fillColor: '#00e676', fillOpacity: 1 })
                            .bindPopup(`<strong>Origin</strong><br>${j.origin}`).addTo(layerGroup);
                        L.circleMarker(dst, { radius: 6, color, fillColor: '#ff1744', fillOpacity: 1 })
                            .bindPopup(`<strong>Dest</strong><br>${j.destination}`).addTo(layerGroup);
                        journeyLegendHtml += `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
                            <div style="width:28px;height:3px;background:${color};border-radius:2px;flex-shrink:0"></div>
                            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${j.origin} → ${j.destination}</span>
                            <span style="color:${color};font-size:11px;flex-shrink:0">${j.status}</span>
                        </div>`;
                    }
                });
                const legendEl = document.getElementById('map-legend');
                if (legendEl) legendEl.innerHTML = journeyLegendHtml
                    || '<div style="color:var(--text-muted);font-size:12px">No journeys with location data.</div>';
            }
        } catch(mapErr) {
            console.warn('Map update failed:', mapErr);
        }
    } catch(err) {
        console.error('loadJourneys error:', err);
    }
}

// =============================================
// BUG FIX 4: Vehicle management
// =============================================
async function loadVehicles() {
    try {
        const r = await authFetch('/api/users/vehicles');
        if (r.status === 401) { localStorage.clear(); location.reload(); return; }
        if (!r.ok) return;
        const data = await r.json();
        const vehicles = data.vehicles || [];

        // Update vehicle list UI
        const list = document.getElementById('vehicle-list');
        if (vehicles.length === 0) {
            list.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:16px;">No vehicles registered. Add one to book journeys.</div>';
        } else {
            list.innerHTML = vehicles.map(v => `
                <div class="data-item">
                    <div>
                        <div style="font-weight:600;font-size:15px">${v.registration}</div>
                        <div style="font-size:12px;color:var(--text-muted)">${v.vehicle_type}</div>
                    </div>
                    <button class="btn btn-sm btn-danger" onclick="removeVehicle('${v.id}')">Remove</button>
                </div>
            `).join('');
        }

        // Update the booking form vehicle dropdown
        const select = document.getElementById('j-vehicle');
        select.innerHTML = '<option value="">Select a registered vehicle...</option>' +
            vehicles.map(v => `<option value="${v.registration}|${v.vehicle_type}">${v.registration} (${v.vehicle_type})</option>`).join('');

    } catch(err) {
        console.error('loadVehicles error:', err);
    }
}

function showAddVehicle() {
    const form = document.getElementById('add-vehicle-form');
    const isShowing = form.style.display !== 'block';
    form.style.display = isShowing ? 'block' : 'none';
    // Pre-fill the plate field with the user's license number on first open
    if (isShowing) {
        const plateInput = document.getElementById('v-plate');
        if (plateInput && !plateInput.value && user && user.license_number) {
            plateInput.value = user.license_number;
        }
    }
}

async function addVehicle() {
    const plate = document.getElementById('v-plate').value.trim();
    const vtype = document.getElementById('v-type').value;
    if (!plate) return showToast("Enter a registration plate", "error");

    try {
        const r = await authFetch('/api/users/vehicles', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ registration: plate, vehicle_type: vtype })
        });
        const d = await r.json();
        if (!r.ok) throw new Error(parseErrorDetail(d));
        showToast(`Vehicle ${d.registration} registered!`, "success");
        document.getElementById('v-plate').value = '';
        document.getElementById('add-vehicle-form').style.display = 'none';
        await loadVehicles();
    } catch(err) { showToast(err.message, "error"); }
}

async function removeVehicle(vehicleId) {
    try {
        const r = await authFetch(`/api/users/vehicles/${vehicleId}`, { method: 'DELETE' });
        if (!r.ok && r.status !== 204) {
            const d = await r.json();
            throw new Error(parseErrorDetail(d));
        }
        showToast("Vehicle removed", "success");
        await loadVehicles();
    } catch(err) { showToast(err.message, "error"); }
}

async function loadDashboard() {
    // Load node health (Archive ALIVE/SUSPECT/DEAD model)
    loadNodeHealth();

    // Load analytics stats
    try {
        const r = await authFetch('/api/analytics/stats');
        if(r.ok) {
            const s = await r.json();
            document.getElementById('stat-total').innerText = s.total_events_today || 0;
            document.getElementById('stat-conf').innerText = s.confirmed_today || 0;
            document.getElementById('stat-rej').innerText = s.rejected_today || 0;
        }
    } catch(e) { console.warn('Stats fetch failed:', e); }
}

// =============================================
// Road Network — fetch predefined Irish routes and draw on map
// Ported from Archive/models/road_network.py (NetworkX graph → Leaflet polylines)
// =============================================

async function loadPredefinedRoutes() {
    try {
        const r = await resilientFetch('/api/conflicts/routes');
        if (!r.ok) return;
        const data = await r.json();
        predefinedRoutes = data.routes || [];
        renderRoadNetwork();
        populateQuickRoutes();
    } catch(err) {
        console.warn('Could not load predefined routes:', err);
    }
}

function renderRoadNetwork() {
    if (!mapInstance) return;
    if (roadNetworkLayer) roadNetworkLayer.remove();

    roadNetworkLayer = L.layerGroup();
    const legendEl = document.getElementById('map-legend');
    if (legendEl) legendEl.innerHTML = '';

    predefinedRoutes.forEach((route, idx) => {
        const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
        const wps = route.waypoints || [];
        if (wps.length >= 2) {
            const latlngs = wps.map(w => [w.lat, w.lng]);
            // Draw the real road path as a polyline
            const line = L.polyline(latlngs, {
                color, weight: 3, opacity: 0.65, dashArray: '8,4'
            }).bindPopup(
                `<strong>${route.name}</strong><br>${route.description || ''}<br>` +
                `Est. ${route.estimated_duration_minutes} min`
            );
            roadNetworkLayer.addLayer(line);

            // Waypoint markers (smaller)
            wps.forEach((wp, wi) => {
                const isEndpoint = wi === 0 || wi === wps.length - 1;
                const circle = L.circleMarker([wp.lat, wp.lng], {
                    radius: isEndpoint ? 6 : 4,
                    color, fillColor: color, fillOpacity: isEndpoint ? 1 : 0.5,
                    weight: isEndpoint ? 2 : 1,
                }).bindPopup(`<strong>${wp.name}</strong><br>${route.name}`);
                roadNetworkLayer.addLayer(circle);
            });
        }

        // Legend entry
        if (legendEl) {
            legendEl.insertAdjacentHTML('beforeend', `
                <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
                    <div style="width:28px;height:3px;background:${color};border-radius:2px;flex-shrink:0"></div>
                    <span>${route.name}</span>
                    <span style="color:var(--text-muted);margin-left:auto">${route.estimated_duration_minutes}min</span>
                </div>
            `);
        }
    });

    if (roadNetworkVisible) roadNetworkLayer.addTo(mapInstance);
}

function toggleRoadNetwork() {
    if (!roadNetworkLayer) return;
    roadNetworkVisible = !roadNetworkVisible;
    if (roadNetworkVisible) {
        roadNetworkLayer.addTo(mapInstance);
        showToast('Road network shown', 'info');
    } else {
        roadNetworkLayer.remove();
        showToast('Road network hidden', 'info');
    }
}

// ── Map mode toggle ───────────────────────────────────────────────────────────
// 'routes'   → predefined road network (default)
// 'journeys' → user's own booked routes
function setMapMode(mode) {
    mapMode = mode;

    // Update button styles
    const btnRoutes   = document.getElementById('btn-mode-routes');
    const btnJourneys = document.getElementById('btn-mode-journeys');
    if (btnRoutes && btnJourneys) {
        const activeStyle   = 'background:var(--primary);color:#fff';
        const inactiveStyle = 'background:transparent;color:var(--text-muted)';
        btnRoutes.style.cssText   = mode === 'routes'   ? activeStyle : inactiveStyle;
        btnJourneys.style.cssText = mode === 'journeys' ? activeStyle : inactiveStyle;
        // Keep btn-primary class on active one for border-radius etc.
        btnRoutes.className   = `btn btn-sm${mode === 'routes'   ? ' btn-primary' : ''}`;
        btnJourneys.className = `btn btn-sm${mode === 'journeys' ? ' btn-primary' : ''}`;
    }

    if (mode === 'routes') {
        // Show road network, clear journey markers
        layerGroup.clearLayers();
        roadNetworkVisible = true;
        if (roadNetworkLayer) roadNetworkLayer.addTo(mapInstance);
        else loadPredefinedRoutes();  // fallback if not loaded yet
        // Re-render legend from road network
        renderRoadNetwork();
    } else {
        // Hide road network, load and display user journeys
        if (roadNetworkLayer) roadNetworkLayer.remove();
        layerGroup.clearLayers();
        loadJourneys();  // will redraw markers + legend when mode === 'journeys'
    }
}

// =============================================
// Quick Route selector — auto-fill coordinates from predefined routes
// Mirrors Archive booking_service.book_journey() route lookup
// =============================================

function populateQuickRoutes() {
    const sel = document.getElementById('j-quick-route');
    if (!sel) return;
    sel.innerHTML = '<option value="">— or use free-form search below —</option>';
    predefinedRoutes.forEach(r => {
        sel.insertAdjacentHTML('beforeend',
            `<option value="${r.route_id}">${r.name} (${r.estimated_duration_minutes} min)</option>`
        );
    });
}

function applyQuickRoute() {
    const sel = document.getElementById('j-quick-route');
    const routeId = sel.value;
    if (!routeId) return;

    const route = predefinedRoutes.find(r => r.route_id === routeId);
    if (!route) return;

    // Auto-fill origin and destination inputs
    document.getElementById('j-origin').value = route.origin_name;
    document.getElementById('j-dest').value = route.destination_name;
    document.getElementById('j-dur').value = route.estimated_duration_minutes;

    // Store as selected locations (used by bookJourney)
    selectedOrigin = {
        name: route.origin_name,
        lat: route.origin_lat,
        lng: route.origin_lng,
    };
    selectedDest = {
        name: route.destination_name,
        lat: route.destination_lat,
        lng: route.destination_lng,
    };

    // Highlight route on map
    if (mapInstance && route.waypoints && route.waypoints.length >= 2) {
        const latlngs = route.waypoints.map(w => [w.lat, w.lng]);
        mapInstance.fitBounds(L.latLngBounds(latlngs), { padding: [40, 40] });
    }
}

// =============================================
// Node Health — Archive ALIVE/SUSPECT/DEAD model
// Auto-refreshes every 10 s while the view is visible
// =============================================

let _nodeHealthTimer = null;

function startNodeHealthPolling() {
    stopNodeHealthPolling();
    loadNodeHealth();
    loadSimStats();
    loadActivityFeed();
    _nodeHealthTimer = setInterval(() => { 
        loadNodeHealth(); 
        loadSimStats(); 
        loadPeerAPIs(); // Periodic discovery of new cluster members
    }, 10000);
    _activityTimer   = setInterval(() => loadActivityFeed(), 5000);
}

function stopNodeHealthPolling() {
    if (_nodeHealthTimer) { clearInterval(_nodeHealthTimer); _nodeHealthTimer = null; }
    if (_activityTimer)   { clearInterval(_activityTimer);   _activityTimer = null; }
}

// ── Cross-node Distributed Activity Feed ─────────────────────────────────────
// Polls /admin/logs from this node AND every ALIVE peer node.
// Merges all entries sorted by timestamp and renders them in #activity-feed.

let _activityTimer = null;
let _activityLastRender = '';  // dedup — skip DOM update if nothing changed

async function loadActivityFeed() {
    const allBases = [API, ...activePeerAPIs.filter(p => p !== API)];
    const allEntries = [];
    let nodesReached = 0;

    // Fetch logs from every node in parallel
    const fetches = allBases.map(async base => {
        try {
            const opts = { headers: { 'Authorization': `Bearer ${token}` } };
            const resp = await fetch(base + '/admin/logs?limit=150', opts);
            if (!resp.ok) return;
            const data = await resp.json();
            (data.entries || []).forEach(e => allEntries.push(e));
            nodesReached++;
        } catch (_) {}
    });
    await Promise.all(fetches);

    if (nodesReached === 0) return;  // all offline, keep last render

    // Sort all entries by timestamp (ISO strings sort lexicographically)
    allEntries.sort((a, b) => a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0);

    // Keep last 300 entries
    const entries = allEntries.slice(-300);

    // Build HTML
    const html = entries.map(e => {
        const t = e.ts ? e.ts.replace('T', ' ').replace(/\.\d+Z$/, 'Z') : '';
        const node = (e.node || '?').slice(0, 12).padEnd(12);
        const svc  = (e.service || '').slice(0, 20).padEnd(20);
        const lvl  = (e.level || 'INFO').slice(0, 5).padEnd(5);
        const msg  = e.msg || '';

        const lvlColors = { ERROR: '#e63946', WARN: '#ffbe0b', WARNING: '#ffbe0b', INFO: '#aaa', DEBUG: '#666' };
        const color = lvlColors[lvl.trim()] || '#aaa';

        // Highlight replication/sync/conflict keywords
        const highlight = (s) => s
            .replace(/\[replication\]/g, '<span style="color:#8338ec">[replication]</span>')
            .replace(/\[sync\]/g, '<span style="color:#00b4d8">[sync]</span>')
            .replace(/CONFIRMED/g, '<span style="color:#06d6a0">CONFIRMED</span>')
            .replace(/REJECTED/g, '<span style="color:#e63946">REJECTED</span>')
            .replace(/PUSH/g, '<span style="color:#8338ec">PUSH</span>')
            .replace(/RECV/g, '<span style="color:#00b4d8">RECV</span>')
            .replace(/CANCEL/g, '<span style="color:#ffbe0b">CANCEL</span>')
            .replace(/CATCH-UP/g, '<span style="color:#00b4d8">CATCH-UP</span>')
            .replace(/SIMULATION/g, '<span style="color:#ff1744">SIMULATION</span>');

        return `<span style="color:${color}"><span style="color:#555">${t}</span> <span style="color:#888">${node}</span> <span style="color:#666">${lvl}</span> ${highlight(msg)}</span>`;
    }).join('\n');

    // Only update DOM if content changed (avoid scroll-jump)
    const key = html.length + (entries[entries.length - 1]?.ts || '');
    if (key === _activityLastRender) return;
    _activityLastRender = key;

    const feedEl = document.getElementById('activity-feed');
    if (!feedEl) return;
    const wasAtBottom = feedEl.scrollHeight - feedEl.scrollTop <= feedEl.clientHeight + 40;
    feedEl.innerHTML = html || '<span style="color:#555">No log entries yet.</span>';
    if (wasAtBottom) feedEl.scrollTop = feedEl.scrollHeight;

    const countEl = document.getElementById('activity-node-count');
    if (countEl) countEl.textContent = `${nodesReached}/${allBases.length} node${allBases.length > 1 ? 's' : ''}`;
}

function clearActivityFeed() {
    _activityLastRender = '';
    const el = document.getElementById('activity-feed');
    if (el) el.innerHTML = '<span style="color:#555">Cleared.</span>';
}

async function loadNodeHealth() {
    try {
        const r = await authFetch('/health/nodes');
        if (!r.ok) return;
        const data = await r.json();

        // ── Laptop peers (other physical machines on the network) ──────────
        const laptopPeers = data.laptop_peers || {};
        const laptopTotal = Object.keys(laptopPeers).length;
        const laptopAlive = Object.values(laptopPeers).filter(p => p.status === 'ALIVE').length;

        // ── Internal microservices ─────────────────────────────────────────
        const services = data.peers || {};
        const svcAlive = Object.values(services).filter(p => p.status === 'ALIVE').length;
        const svcTotal = Object.keys(services).length;

        // LOCAL ONLY badge
        const badge = document.getElementById('local-only-badge');
        if (badge) badge.style.display = data.local_only_mode ? 'inline-block' : 'none';

        // Laptop count label
        const simCount = document.getElementById('sim-node-count');
        if (simCount) simCount.textContent = laptopTotal > 0
            ? `${laptopAlive}/${laptopTotal} alive`
            : 'none registered';

        // ── Helper: build a health card ────────────────────────────────────
        function buildCard(name, info) {
            const colors = { ALIVE: '#06d6a0', SUSPECT: '#ffbe0b', DEAD: '#e63946' };
            const c = colors[info.status] || '#888';
            const lastSeen = info.last_seen_s_ago < 60
                ? `${info.last_seen_s_ago}s ago`
                : `${Math.round(info.last_seen_s_ago / 60)}m ago`;
            return `<div style="background:var(--card-bg);border:2px solid ${c};border-radius:8px;padding:12px">
                <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px;word-break:break-all">${name}</div>
                <div style="font-weight:700;color:${c};font-size:15px;margin-bottom:4px">${info.status}</div>
                <div style="font-size:11px;color:var(--text-muted)">seen ${lastSeen}</div>
                ${info.consecutive_failures > 0
                    ? `<div style="font-size:11px;color:var(--danger);margin-top:2px">${info.consecutive_failures} missed ping${info.consecutive_failures > 1 ? 's' : ''}</div>`
                    : ''}
            </div>`;
        }

        // ── Laptop peers grid ──────────────────────────────────────────────
        const laptopHtml = Object.entries(laptopPeers).map(([name, info]) => buildCard(name, info)).join('')
            || '<div style="color:var(--text-muted);font-size:13px">No laptops registered yet. Use the form below to add a teammate\'s node.</div>';

        // ── Internal services grid ─────────────────────────────────────────
        const svcHtml = Object.entries(services).map(([name, info]) => buildCard(name, info)).join('')
            || '<div style="color:var(--text-muted);font-size:13px">No internal services detected.</div>';

        const laptopGrid = document.getElementById('sim-node-health-grid');
        if (laptopGrid) laptopGrid.innerHTML = laptopHtml;

        const svcGrid = document.getElementById('sim-svc-grid');
        if (svcGrid) svcGrid.innerHTML = svcHtml;

    } catch(err) {
        console.warn('Node health fetch failed:', err);
    }
}

// =============================================
// Simulation stats bar
// =============================================

async function loadSimStats() {
    try {
        const r = await authFetch('/admin/simulate/status');
        const data = r.ok ? await r.json() : {};
        // data.peers = laptop peer statuses only (not internal services)
        const laptopPeers = Object.values(data.peers || {});
        const alive = laptopPeers.filter(p => p.status === 'ALIVE').length;
        const suspect = laptopPeers.filter(p => p.status === 'SUSPECT').length;
        const dead = laptopPeers.filter(p => p.status === 'DEAD').length;

        const selfEl = document.getElementById('sim-self-status');
        if (selfEl) selfEl.innerHTML = data.node_failed
            ? '<span style="color:var(--danger)">💀 FAILED</span>'
            : '<span style="color:var(--success)">🟢 ALIVE</span>';

        const aliveEl = document.getElementById('sim-alive');
        if (aliveEl) aliveEl.textContent = alive;

        const sdEl = document.getElementById('sim-suspect-dead');
        if (sdEl) sdEl.textContent = `${suspect} / ${dead}`;

        const modeEl = document.getElementById('sim-mode');
        if (modeEl) modeEl.innerHTML = data.local_only_mode
            ? '<span style="color:var(--danger)">🔴 LOCAL</span>'
            : '<span style="color:var(--success)">🌐 GLOBAL</span>';

        // Keep kill/recover buttons reflecting current state
        const killBtn = document.getElementById('btn-kill-node');
        const recBtn = document.getElementById('btn-recover-node');
        if (killBtn) killBtn.disabled = !!data.node_failed;
        if (recBtn) recBtn.disabled = !data.node_failed;
    } catch(e) { console.warn('loadSimStats:', e); }
}

function simLog(msg, type = 'info') {
    const el = document.getElementById('sim-log');
    if (!el) return;
    const now = new Date().toLocaleTimeString();
    const colors = {info:'#aaa', success:'#06d6a0', error:'#e63946', warn:'#ffbe0b'};
    el.innerHTML += `<span style="color:${colors[type]||'#aaa'}">[${now}] ${msg}\n</span>`;
    el.scrollTop = el.scrollHeight;
}

async function simDemo(type) {
    simLog(`Starting demo: ${type}`, 'info');

    if (type === 'consistency') {
        // Two concurrent bookings for same route/time
        simLog('Firing two concurrent bookings for same route and departure time…', 'warn');
        const route = predefinedRoutes[0];
        if (!route) { simLog('No routes loaded', 'error'); return; }
        const dep = new Date(Date.now() + 90*60*1000).toISOString();
        const payload = {
            origin: route.origin_name, destination: route.destination_name,
            origin_lat: route.origin_lat, origin_lng: route.origin_lng,
            destination_lat: route.destination_lat, destination_lng: route.destination_lng,
            departure_time: dep, estimated_duration_minutes: route.estimated_duration_minutes,
            vehicle_registration: 'SIM-A001', vehicle_type: 'CAR',
        };
        const [r1, r2] = await Promise.all([
            authFetch('/api/journeys/', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload, vehicle_registration:'SIM-A001'})}),
            authFetch('/api/journeys/', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload, vehicle_registration:'SIM-A002'})}),
        ]);
        const d1 = await r1.json();
        const d2 = await r2.json();
        simLog(`  DRIVER-A: ${d1.status} ${d1.rejection_reason||''}`, d1.status==='CONFIRMED'?'success':'warn');
        simLog(`  DRIVER-B: ${d2.status} ${d2.rejection_reason||''}`, d2.status==='CONFIRMED'?'success':'warn');
        const wins = [d1,d2].filter(d => d.status==='CONFIRMED').length;
        simLog(wins <= 1 ? '✅ Conflict detection working' : '⚠ Multiple writes accepted!', wins<=1?'success':'error');

    } else if (type === '2pc') {
        simLog('Booking with 2PC TCC coordinator (mode=2pc)…', 'info');
        const route = predefinedRoutes[1] || predefinedRoutes[0];
        if (!route) { simLog('No routes loaded', 'error'); return; }
        const dep = new Date(Date.now() + 120*60*1000).toISOString();
        const r = await authFetch('/api/journeys/?mode=2pc', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({
                origin: route.origin_name, destination: route.destination_name,
                origin_lat: route.origin_lat, origin_lng: route.origin_lng,
                destination_lat: route.destination_lat, destination_lng: route.destination_lng,
                departure_time: dep, estimated_duration_minutes: route.estimated_duration_minutes,
                vehicle_registration: 'TPC-DEMO01', vehicle_type: 'CAR',
            })
        });
        const d = await r.json();
        simLog(`  Status: ${d.status}  id=${d.id||'?'}`, d.status==='CONFIRMED'?'success':'warn');
        if (d.rejection_reason) simLog(`  Reason: ${d.rejection_reason}`, 'warn');
        simLog(d.status==='CONFIRMED'
            ? '✅ 2PC COMMITTED — capacity reserved + journey confirmed atomically'
            : '⚠ 2PC ABORTED — compensating CANCEL issued to conflict-service',
            d.status==='CONFIRMED'?'success':'warn');

    } else if (type === 'storm') {
        simLog('Firing 10 concurrent bookings (storm test)…', 'warn');
        const reqs = Array.from({length:10}, (_, i) => {
            const route = predefinedRoutes[i % predefinedRoutes.length] || predefinedRoutes[0];
            const dep = new Date(Date.now() + (30 + i*15)*60*1000).toISOString();
            return authFetch('/api/journeys/', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                    origin: route.origin_name, destination: route.destination_name,
                    origin_lat: route.origin_lat, origin_lng: route.origin_lng,
                    destination_lat: route.destination_lat, destination_lng: route.destination_lng,
                    departure_time: dep, estimated_duration_minutes: route.estimated_duration_minutes,
                    vehicle_registration: `STRM-${String(i).padStart(3,'0')}`, vehicle_type: 'CAR',
                })
            });
        });
        const results = await Promise.all(reqs.map(p => p.then(r => r.json()).catch(e => ({status:'ERROR',rejection_reason:String(e)}))));
        const ok = results.filter(d => d.status === 'CONFIRMED').length;
        results.forEach((d,i) => simLog(`  [${i}] ${d.status} ${d.rejection_reason||''}`.slice(0,80), d.status==='CONFIRMED'?'success':'warn'));
        simLog(`Storm done: ${ok}/10 confirmed`, ok>0?'success':'warn');

    } else if (type === 'outbox') {
        simLog('Forcing outbox drain to RabbitMQ…', 'info');
        try {
            const r = await authFetch('/admin/recovery/drain-outbox', {method:'POST'});
            const d = await r.json();
            simLog(`  Drained ${d.events_drained||0} event(s)`, 'success');
        } catch(e) { simLog(`  Error: ${e}`, 'error'); }
    }

    await loadSimStats();
    await loadNodeHealth();
}

// =============================================
// Node failure simulation (Archive simulate_node_failure / simulate_node_recovery)
// =============================================

async function simKillNode() {
    simLog('Sending KILL signal to this node…', 'warn');
    try {
        const r = await authFetch('/admin/simulate/fail', {method: 'POST'});
        const d = await r.json();
        simLog(`💀 ${d.message}`, 'error');
        showToast('Node failure simulated — peers will detect SUSPECT in ~30s', 'error');
        await loadSimStats(); await loadNodeHealth();
    } catch(e) { simLog(`Error: ${e}`, 'error'); }
}

async function simRecoverNode() {
    simLog('Sending RECOVER signal to this node…', 'info');
    try {
        const r = await authFetch('/admin/simulate/recover', {method: 'POST'});
        const d = await r.json();
        simLog(`💚 ${d.message}`, 'success');
        showToast('Node recovered — peers will detect ALIVE on next heartbeat', 'success');
        await loadSimStats(); await loadNodeHealth();
    } catch(e) { simLog(`Error: ${e}`, 'error'); }
}

async function simRegisterPeer() {
    const name = document.getElementById('peer-name').value.trim();
    const url = document.getElementById('peer-url').value.trim();
    if (!name || !url) { showToast('Enter both name and health URL', 'error'); return; }
    try {
        const r = await authFetch('/admin/peers/register', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, health_url: url})
        });
        const d = await r.json();
        if (d.is_new) {
            simLog(`Registered peer '${d.registered}' → ${d.health_url}`, 'success');
            showToast(`Peer '${name}' added permanently`, 'success');
        } else {
            const labelStr = d.label ? ` (Registered as ${d.label})` : "";
            simLog(`Peer '${d.registered}' is already in the cluster configuration${labelStr}`, 'info');
            showToast(`${name} is already registered${labelStr}`, 'info');
        }
        simLog(`  ${d.note}`, 'info');
        document.getElementById('peer-name').value = '';
        document.getElementById('peer-url').value = '';
        await loadNodeHealth();
        await loadPeerAPIs(); // refresh failover peer list
    } catch(e) { simLog(`Error registering peer: ${e}`, 'error'); }
}

async function simDrainOutbox() {
    simLog('Force-draining outbox…', 'info');
    try {
        const r = await authFetch('/admin/recovery/drain-outbox', {method:'POST'});
        const d = await r.json();
        simLog(`Drained ${d.events_drained||0} event(s) to RabbitMQ`, 'success');
        showToast(`Drained ${d.events_drained||0} outbox events`, 'success');
    } catch(e) { simLog(`Error: ${e}`, 'error'); }
}

async function simRebuildCache() {
    simLog('Rebuilding enforcement Redis cache…', 'info');
    try {
        const r = await authFetch('/admin/recovery/rebuild-enforcement-cache', {method:'POST'});
        const d = await r.json();
        simLog(`Cached ${d.journeys_cached||0} active journeys`, 'success');
        showToast(`Cached ${d.journeys_cached||0} journeys`, 'success');
    } catch(e) { simLog(`Error: ${e}`, 'error'); }
}

// =============================================
// Enforcement Agent — booking verification
// =============================================

function enfSwitchTab(tab) {
    document.getElementById('enf-form-vehicle').style.display = tab === 'vehicle' ? 'block' : 'none';
    document.getElementById('enf-form-license').style.display = tab === 'license'  ? 'block' : 'none';
    document.getElementById('enf-tab-vehicle').classList.toggle('active', tab === 'vehicle');
    document.getElementById('enf-tab-license').classList.toggle('active', tab === 'license');
    document.getElementById('enf-result').style.display = 'none';
}

async function enfLookup(type) {
    const query = type === 'vehicle'
        ? document.getElementById('enf-plate').value.trim()
        : document.getElementById('enf-license').value.trim();

    if (!query) {
        showToast(type === 'vehicle' ? 'Enter a vehicle plate' : 'Enter a licence number', 'error');
        return;
    }

    const url = type === 'vehicle'
        ? `/api/enforcement/verify/vehicle/${encodeURIComponent(query)}`
        : `/api/enforcement/verify/license/${encodeURIComponent(query)}`;

    try {
        const r = await authFetch(url);
        if (r.status === 403) {
            showToast('Access denied — Enforcement Agent role required', 'error');
            return;
        }
        const stale = r.headers.get('X-Data-Staleness') === 'STALE';
        const d = await r.json();
        if (!r.ok) throw new Error(parseErrorDetail(d));
        renderEnfResult(d, query, type, stale);
    } catch(err) {
        showToast('Verification failed: ' + err.message, 'error');
    }
}

function renderEnfResult(d, query, type, stale) {
    const resultEl = document.getElementById('enf-result');
    const titleEl  = document.getElementById('enf-result-title');
    const bodyEl   = document.getElementById('enf-result-body');

    const valid = d.is_valid;
    const statusColor = valid ? 'var(--success)' : 'var(--danger)';
    const statusText  = valid ? 'ACTIVE BOOKING FOUND' : 'NO ACTIVE BOOKING';
    const statusIcon  = valid ? '✅' : '🚫';

    titleEl.innerHTML = `${statusIcon} ${type === 'vehicle' ? 'Vehicle' : 'Driver'}: <span style="color:${statusColor}">${statusText}</span>`;

    const rows = [
        ['Queried', type === 'vehicle' ? `Plate: ${query}` : `Licence: ${query}`],
        ['Checked at', d.checked_at ? new Date(d.checked_at).toLocaleString() : '—'],
        ...(valid ? [
            ['Journey ID',  d.journey_id   || '—'],
            ['Status',      d.journey_status || '—'],
            ['Route',       d.origin && d.destination ? `${d.origin} → ${d.destination}` : '—'],
            ['Departure',   d.departure_time ? new Date(d.departure_time).toLocaleString() : '—'],
            ['Est. Arrival',d.estimated_arrival_time ? new Date(d.estimated_arrival_time).toLocaleString() : '—'],
            ['Driver ID',   d.driver_id || '—'],
        ] : []),
    ];

    const tableHtml = `
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px">
            ${rows.map(([k, v]) => `
                <tr style="border-bottom:1px solid rgba(255,255,255,0.05)">
                    <td style="padding:8px 0;color:var(--text-muted);width:40%">${k}</td>
                    <td style="padding:8px 0;font-weight:500">${v}</td>
                </tr>
            `).join('')}
        </table>
        ${stale ? `<div style="margin-top:12px;padding:8px 12px;background:rgba(255,234,0,0.1);border-left:3px solid var(--warning);border-radius:4px;font-size:12px;color:var(--warning)">
            ⚠ Data may be stale — Journey Service is currently partitioned. Results are from local cache.
        </div>` : ''}
    `;

    bodyEl.innerHTML = tableHtml;
    resultEl.style.display = 'block';
}

// =============================================
// Toast notification system
// =============================================
function showToast(message, type = "info") {
    toastCounter++;
    const id = `toast-${toastCounter}`;
    const colors = {
        success: 'var(--success)',
        error: 'var(--danger)',
        info: 'var(--primary)',
        warning: 'var(--warning)'
    };
    const borderColor = colors[type] || colors.info;

    const toast = document.createElement('div');
    toast.id = id;
    toast.className = 'toast-notification';
    toast.style.borderLeftColor = borderColor;
    toast.innerHTML = `<span>${message}</span><button class="toast-close" onclick="this.parentElement.remove()">&times;</button>`;

    const container = document.getElementById('toast-container');
    container.appendChild(toast);

    setTimeout(() => {
        const el = document.getElementById(id);
        if (el) {
            el.style.opacity = '0';
            el.style.transform = 'translateX(50px)';
            setTimeout(() => el.remove(), 300);
        }
    }, 7000);
}
