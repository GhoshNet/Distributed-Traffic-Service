const API = 'http://localhost:8080';
const WS = 'ws://localhost:8080';

const CITIES = [
  { name: 'Dublin', lat: 53.3498, lng: -6.2603 },
  { name: 'Cork', lat: 51.8985, lng: -8.4756 },
  { name: 'Galway', lat: 53.2707, lng: -9.0568 },
  { name: 'Limerick', lat: 52.6638, lng: -8.6267 },
  { name: 'Waterford', lat: 52.2593, lng: -7.1101 },
  { name: 'Belfast', lat: 54.5973, lng: -5.9301 }
];

let token = localStorage.getItem('jb_token');
let user = JSON.parse(localStorage.getItem('jb_user') || 'null');
let wsConn = null;
let mapInstance = null;
let markers = {};
let layerGroup = null;

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
    if (!r.ok) throw new Error(d.detail);
    token = d.access_token; localStorage.setItem('jb_token', token);
    const p = await fetch(API+'/api/users/me', {headers:{'Authorization': `Bearer ${token}`}});
    user = await p.json(); localStorage.setItem('jb_user', JSON.stringify(user));
    enterApp();
  } catch(err) { showToast("Login failed: " + err.message, "error"); }
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
        if(!r.ok) throw new Error((await r.json()).detail);
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
  populateDropdowns();
}

function go(view) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(`view-${view}`).classList.add('active');
  document.getElementById(`nav-${view}`).classList.add('active');

  if(view === 'map') setTimeout(() => mapInstance.invalidateSize(), 300);
  if(view === 'dash') loadDashboard();
  if(view === 'journeys') { loadJourneys(); }
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

async function authFetch(url, opts={}) {
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = `Bearer ${token}`;
  return fetch(API+url, opts);
}

function populateDropdowns() {
    const opts = CITIES.map(c => `<option value="${c.name}">${c.name}</option>`);
    document.getElementById('j-origin').innerHTML = '<option value="">Origin...</option>' + opts.join('');
    document.getElementById('j-dest').innerHTML = '<option value="">Dest...</option>' + opts.join('');
    
    let d = new Date(); d.setHours(d.getHours()+1);
    document.getElementById('j-depart').value = d.toISOString().slice(0,16);
}

async function bookJourney(e) {
    e.preventDefault();
    const origin = CITIES.find(c => c.name === document.getElementById('j-origin').value);
    const dest = CITIES.find(c => c.name === document.getElementById('j-dest').value);
    if(!origin || !dest) return showToast("Select origin and destination", "error");

    const payload = {
        origin: origin.name, destination: dest.name,
        origin_lat: origin.lat, origin_lng: origin.lng,
        destination_lat: dest.lat, destination_lng: dest.lng,
        departure_time: new Date(document.getElementById('j-depart').value).toISOString(),
        estimated_duration_minutes: parseInt(document.getElementById('j-dur').value),
        vehicle_registration: document.getElementById('j-plate').value.toUpperCase(),
        vehicle_type: document.getElementById('j-vtype').value
    };

    try {
        const r = await authFetch('/api/journeys/', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if(!r.ok) throw new Error(d.detail);
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

        // Update map with confirmed/in-progress journeys
        try {
            layerGroup.clearLayers();
            journeys.forEach(j => {
                if((j.status === "CONFIRMED" || j.status === "IN_PROGRESS") && j.origin_lat) {
                    const org = [j.origin_lat, j.origin_lng];
                    const dst = [j.destination_lat, j.destination_lng];
                    L.polyline([org, dst], {color: '#8a2be2', weight: 3, opacity: 0.7, dashArray: '5,10'}).addTo(layerGroup);
                    L.circleMarker(org, {radius:6, color:'#00e676', fillOpacity:1}).bindPopup(`Origin: ${j.origin}`).addTo(layerGroup);
                    L.circleMarker(dst, {radius:6, color:'#ff1744', fillOpacity:1}).bindPopup(`Dest: ${j.destination}`).addTo(layerGroup);
                }
            });
        } catch(mapErr) {
            console.warn('Map update failed:', mapErr);
        }
    } catch(err) {
        console.error('loadJourneys error:', err);
    }
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
