const API = 'http://localhost:8080';
const WS = 'ws://localhost:8080';

let token = localStorage.getItem('jb_token');
let user = JSON.parse(localStorage.getItem('jb_user') || 'null');
let wsConn = null;
let mapInstance = null;
let markers = {};
let layerGroup = null;

// Geocoding state — stores the selected location objects
let selectedOrigin = null;
let selectedDest = null;
let geocodeTimers = {};

// Routing logic
if (token && user) enterApp();
else document.getElementById('auth-screen').style.display = 'flex';

function switchAuth(tab) {
  document.getElementById('login-form').style.display = tab === 'login' ? 'block' : 'none';
  document.getElementById('register-form').style.display = tab === 'register' ? 'block' : 'none';
  document.querySelectorAll('.auth-tab').forEach((el, i) => el.classList.toggle('active', (tab==='login')===(i===0)));
}

async function login(e) {
  e.preventDefault();
  try {
    const r = await fetch(API+'/api/users/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: e.target[0].value, password: e.target[1].value})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(parseErrorDetail(d));
    token = d.access_token; localStorage.setItem('jb_token', token);
    const p = await fetch(API+'/api/users/me', {headers:{'Authorization': `Bearer ${token}`}});
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
        const r = await fetch(API+'/api/users/register', {
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

function enterApp() {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  document.getElementById('user-name').innerText = user.full_name;
  initMap();
  go('map');
  connectWS();
  setupAutocomplete();
  loadVehicles();
  // Set default departure time
  let d = new Date(); d.setHours(d.getHours()+1);
  document.getElementById('j-depart').value = d.toISOString().slice(0,16);
}

function go(view) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(`view-${view}`).classList.add('active');
  document.getElementById(`nav-${view}`).classList.add('active');

  // BUG FIX 2: Load journeys on map view too so markers appear
  if(view === 'map') {
    setTimeout(() => mapInstance.invalidateSize(), 300);
    loadJourneys();
  }
  if(view === 'dash') loadDashboard();
  if(view === 'journeys') { loadJourneys(); loadVehicles(); }
}

function initMap() {
  mapInstance = L.map('map').setView([53.1424, -7.6921], 7);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(mapInstance);
  layerGroup = L.layerGroup().addTo(mapInstance);
}

function connectWS() {
  wsConn = new WebSocket(`${WS}/ws/notifications/?token=${token}`);
  wsConn.onopen = () => document.getElementById('ws-dot').className = 'ws-dot connected';
  wsConn.onclose = () => {
    document.getElementById('ws-dot').className = 'ws-dot';
    setTimeout(connectWS, 5000);
  };
  wsConn.onmessage = e => {
    if(e.data === 'pong') return;
    try { 
      const data = JSON.parse(e.data);
      handleLiveEvent(data);
    } catch {}
  };
  setInterval(()=> wsConn.readyState===1 && wsConn.send('ping'), 25000);
}

function handleLiveEvent(data) {
    // Show toast
    const toast = document.createElement('div');
    toast.className = 'event-toast';
    toast.innerHTML = `<strong>${data.title}</strong><div style="font-size:12px;opacity:0.8;margin-top:4px">${data.message}</div>`;
    document.body.appendChild(toast);
    setTimeout(()=> toast.remove(), 5000);

    // Draw the confirmed journey route on the live map
    if(data.event_type === "journey.confirmed" && data.metadata) {
        const m = data.metadata;
        if(m.origin_lat) drawRoute(m, layerGroup);
    }

    // Refresh journey list when we get journey events via WebSocket
    if(data.event_type && data.event_type.startsWith("journey.")) {
        loadJourneys();
    }
}

// Draw a journey route on the given Leaflet layer group.
// Priority: 1) stored GeoJSON geometry from backend  2) OSRM live fetch  3) straight-line fallback
async function drawRoute(j, layerRef) {
    const org = [j.origin_lat, j.origin_lng];
    const dst = [j.destination_lat, j.destination_lng];
    const routeStyle = {color: '#8a2be2', weight: 3, opacity: 0.85};

    L.circleMarker(org, {radius:7, color:'#00e676', fillColor:'#00e676', fillOpacity:1})
        .bindPopup(`<b>Origin</b><br>${j.origin || ''}`)
        .addTo(layerRef);
    L.circleMarker(dst, {radius:7, color:'#ff1744', fillColor:'#ff1744', fillOpacity:1})
        .bindPopup(`<b>Destination</b><br>${j.destination || ''}`)
        .addTo(layerRef);

    // 1. Use stored server-side geometry (computed by journey service via OSRM at booking time)
    if (j.route_geometry) {
        try {
            const geom = typeof j.route_geometry === 'string' ? JSON.parse(j.route_geometry) : j.route_geometry;
            L.geoJSON(geom, {style: () => routeStyle}).addTo(layerRef);
            return;
        } catch(e) { console.warn('Failed to parse stored route geometry:', e); }
    }

    // 2. Fetch from OSRM directly (actual road network, no API key required)
    try {
        const url = `https://router.project-osrm.org/route/v1/driving/${j.origin_lng},${j.origin_lat};${j.destination_lng},${j.destination_lat}?overview=full&geometries=geojson`;
        const r = await fetch(url);
        if (r.ok) {
            const data = await r.json();
            if (data.routes && data.routes[0]) {
                L.geoJSON(data.routes[0].geometry, {style: () => routeStyle}).addTo(layerRef);
                return;
            }
        }
    } catch(e) { console.warn('OSRM routing unavailable, using straight line:', e); }

    // 3. Fallback: straight line
    L.polyline([org, dst], {...routeStyle, dashArray: '5,10'}).addTo(layerRef);
}

async function authFetch(url, opts={}) {
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = `Bearer ${token}`;
  return fetch(API+url, opts);
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

    const payload = {
        origin: selectedOrigin.name, destination: selectedDest.name,
        origin_lat: selectedOrigin.lat, origin_lng: selectedOrigin.lng,
        destination_lat: selectedDest.lat, destination_lng: selectedDest.lng,
        departure_time: new Date(document.getElementById('j-depart').value).toISOString(),
        estimated_duration_minutes: parseInt(document.getElementById('j-dur').value),
        vehicle_registration: plate,
        vehicle_type: vtype
    };

    try {
        const r = await authFetch('/api/journeys/', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if(!r.ok) throw new Error(parseErrorDetail(d));
        if(d.status === "REJECTED") {
            showToast(`Rejected: ${d.rejection_reason}`, "error");
        } else {
            showToast("Journey booked successfully!", "success");
            // Draw the actual road route immediately from the POST response —
            // avoids read-replica lag; uses server-stored geometry if OSRM succeeded.
            if(d.origin_lat && layerGroup) drawRoute(d, layerGroup);
        }
        // Render the newly created journey immediately from the POST response
        appendJourneyToList(d);
        // Then refresh the full list from the server
        await loadJourneys();
    } catch(err) { showToast(err.message, "error"); }
}

function renderJourneyItem(j) {
    return `<div class="data-item">
        <div>
            <div style="font-weight:600;font-size:15px;margin-bottom:4px">${j.origin} → ${j.destination}</div>
            <div style="font-size:12px;color:var(--text-muted)">${new Date(j.departure_time).toLocaleString()} | ${j.vehicle_registration} (${j.vehicle_type})</div>
            ${j.rejection_reason ? `<div style="font-size:12px;color:var(--warning);margin-top:4px">Reason: ${j.rejection_reason}</div>` : ''}
        </div>
        <div>
            <span class="badge badge-${j.status.toLowerCase()}">${j.status}</span>
        </div>
    </div>`;
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

        // Re-render all active routes on the map using actual road geometry.
        try {
            layerGroup.clearLayers();
            journeys
                .filter(j => (j.status === "CONFIRMED" || j.status === "IN_PROGRESS") && j.origin_lat)
                .forEach(j => drawRoute(j, layerGroup)); // async — routes appear progressively
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
    form.style.display = form.style.display === 'none' ? 'block' : 'none';
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
    // Load analytics stats
    const r = await authFetch('/api/analytics/stats');
    if(r.ok) {
        const s = await r.json();
        document.getElementById('stat-total').innerText = s.total_events_today || 0;
        document.getElementById('stat-conf').innerText = s.confirmed_today || 0;
        document.getElementById('stat-rej').innerText = s.rejected_today || 0;
    }

    // Load points balance
    try {
        const pr = await authFetch('/api/journeys/points/balance');
        if(pr.ok) {
            const pts = await pr.json();
            document.getElementById('stat-points').innerText = pts.balance || 0;
        }
    } catch(e) { console.warn('Points fetch failed:', e); }

    // Load points history
    try {
        const hr = await authFetch('/api/journeys/points/history?limit=10');
        if(hr.ok) {
            const data = await hr.json();
            const list = document.getElementById('points-history');
            if(data.transactions && data.transactions.length > 0) {
                list.innerHTML = data.transactions.map(t => {
                    const isPositive = t.amount > 0;
                    const color = isPositive ? 'var(--success)' : 'var(--danger)';
                    const sign = isPositive ? '+' : '';
                    const reasonLabel = t.reason.replace(/_/g, ' ');
                    const date = t.created_at ? new Date(t.created_at).toLocaleString() : '';
                    return `<div class="data-item">
                        <div>
                            <div style="font-weight:600;font-size:14px">${reasonLabel}</div>
                            <div style="font-size:12px;color:var(--text-muted)">${date}</div>
                        </div>
                        <div style="font-weight:700;font-size:16px;color:${color}">${sign}${t.amount}</div>
                    </div>`;
                }).join('');
            } else {
                list.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:16px;">No points history yet.</div>';
            }
        }
    } catch(e) { console.warn('Points history fetch failed:', e); }
}

// Toast notification system
let toastCounter = 0;
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
    }, 4000);
}
