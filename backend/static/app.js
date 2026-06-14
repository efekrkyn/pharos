const WS_URL = `ws://${location.host}/ws`;
const RECONNECT_DELAY_MS = 1000;

// Default view before we have a real position fix: TED University, Ankara.
const DEFAULT_VIEW = [39.9228214, 32.8618589];
const DEFAULT_ZOOM = 16;

// Cap how many trail points we keep so each polyline stays lightweight.
const MAX_TRAIL_POINTS = 200;

// Rolling window: how many telemetry samples each chart keeps before old
// points scroll off the left edge.
const MAX_CHART_POINTS = 120;

// The swarm: ids match the backend's DRONES keys, labels match the "label"
// field the backend sends in each telemetry message.
const DRONE_IDS = ["drone0", "drone1", "drone2"];
const DRONE_LABELS = { drone0: "D0", drone1: "D1", drone2: "D2" };

// Currently-selected drone: the single-drone panels (telemetry, charts,
// flight commands, manual control, mission planning, geofence) all operate
// on this one.
let selectedDrone = "drone0";

const statusDotEl = document.getElementById("status-dot");
const statusTextEl = document.getElementById("status-text");
const latEl = document.getElementById("lat");
const lonEl = document.getElementById("lon");
const absAltEl = document.getElementById("abs-alt");
const relAltEl = document.getElementById("rel-alt");
const controllingDroneLabelEl = document.getElementById("controlling-drone-label");
const dronesListEl = document.getElementById("drones-list");

const map = L.map("map").setView(DEFAULT_VIEW, DEFAULT_ZOOM);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: "abcd",
  maxZoom: 20,
}).addTo(map);

function formatNumber(value, digits) {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

function setStatus(cssClass, text) {
  statusDotEl.className = `status-dot ${cssClass}`;
  statusTextEl.textContent = text;
}

// --- Per-drone runtime state ----------------------------------------------

function waitingTelemetry(droneId) {
  return {
    drone_id: droneId,
    label: DRONE_LABELS[droneId],
    status: "waiting",
    lat: null,
    lon: null,
    abs_alt: null,
    rel_alt: null,
    mission_current: 0,
    mission_total: 0,
    heading_deg: null,
    ground_speed_m_s: null,
    battery_percent: null,
    battery_voltage_v: null,
  };
}

// One entry per drone: live telemetry, map marker/trail, and chart history.
const drones = {};
for (const id of DRONE_IDS) {
  drones[id] = {
    latest: waitingTelemetry(id),
    marker: null,
    trail: null,
    trailPoints: [],
    chart: { altitude: [], speed: [], battery: [], batteryEverSeen: false },
    manualEnabled: false,
  };
}

// Snaps the map to the first real position fix received from any drone.
let initialViewSet = false;

const TRAIL_COLORS = { drone0: "#2dd4bf", drone1: "#22d3ee", drone2: "#f472b6" };

function droneMarkerIcon(droneId) {
  return L.divIcon({
    className: "drone-marker",
    html: `<div class="drone-dot drone-color-${droneId}"></div><div class="drone-marker-label">${DRONE_LABELS[droneId]}</div>`,
    iconSize: [40, 34],
    iconAnchor: [8, 8],
  });
}

function updateDroneMarker(droneId, lat, lon) {
  const state = drones[droneId];
  const position = [lat, lon];

  if (state.marker === null) {
    state.marker = L.marker(position, { icon: droneMarkerIcon(droneId) }).addTo(map);
    state.trail = L.polyline([position], { color: TRAIL_COLORS[droneId], weight: 2 }).addTo(map);
    if (!initialViewSet) {
      map.setView(position, DEFAULT_ZOOM);
      initialViewSet = true;
    }
  } else {
    state.marker.setLatLng(position);
  }

  state.trailPoints.push(position);
  if (state.trailPoints.length > MAX_TRAIL_POINTS) {
    state.trailPoints.shift();
  }
  state.trail.setLatLngs(state.trailPoints);

  if (droneId === selectedDrone) {
    map.panTo(position);
  }
}

// Latest vehicle heading (degrees clockwise from north) for the selected
// drone, used to convert the move joystick's map-relative (north/east)
// intent into the drone's forward/right body frame. Stays at 0 (north)
// until telemetry provides it.
let currentHeadingDeg = 0;

// --- Telemetry charts ----------------------------------------------------

const CHART_COLOR = "#2dd4bf";
const CHART_GRID_COLOR = "rgba(230, 244, 241, 0.06)";
const CHART_TEXT_COLOR = "#7a8c8a";

function createTelemetryChart(canvasId, label) {
  const ctx = document.getElementById(canvasId);
  return new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label,
          data: [],
          borderColor: CHART_COLOR,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.25,
          spanGaps: true,
        },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false },
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false },
        y: {
          grid: { color: CHART_GRID_COLOR },
          ticks: { color: CHART_TEXT_COLOR, font: { family: "JetBrains Mono", size: 10 } },
        },
      },
    },
  });
}

const altitudeChart = createTelemetryChart("chart-altitude", "Rel. altitude (m)");
const speedChart = createTelemetryChart("chart-speed", "Ground speed (m/s)");
const batteryChart = createTelemetryChart("chart-battery", "Battery (%)");
const batteryChartStatusEl = document.getElementById("battery-chart-status");

function pushChartPoint(chart, value) {
  const data = chart.data.datasets[0].data;
  chart.data.labels.push("");
  data.push(value);
  if (data.length > MAX_CHART_POINTS) {
    chart.data.labels.shift();
    data.shift();
  }
  chart.update();
}

function setChartData(chart, values) {
  chart.data.labels = values.map(() => "");
  chart.data.datasets[0].data = values.slice();
  chart.update();
}

// Records one telemetry sample into droneId's chart history, and if it's
// the currently-selected drone, also pushes it onto the visible charts.
function pushChartHistory(droneId, data) {
  const history = drones[droneId].chart;
  const connected = data.status === "connected";

  const altitudeValue = connected ? data.rel_alt ?? null : null;
  const speedValue = connected ? data.ground_speed_m_s ?? null : null;
  const batteryValue = connected ? data.battery_percent ?? null : null;
  if (batteryValue !== null) {
    history.batteryEverSeen = true;
  }

  history.altitude.push(altitudeValue);
  history.speed.push(speedValue);
  history.battery.push(batteryValue);
  if (history.altitude.length > MAX_CHART_POINTS) {
    history.altitude.shift();
    history.speed.shift();
    history.battery.shift();
  }

  if (droneId === selectedDrone) {
    pushChartPoint(altitudeChart, altitudeValue);
    pushChartPoint(speedChart, speedValue);
    pushChartPoint(batteryChart, batteryValue);
    batteryChartStatusEl.textContent = history.batteryEverSeen
      ? ""
      : "No battery data reported by this vehicle.";
  }
}

// Replaces the visible charts' data with droneId's history. Called when the
// selected drone changes.
function loadChartsForDrone(droneId) {
  const history = drones[droneId].chart;
  setChartData(altitudeChart, history.altitude);
  setChartData(speedChart, history.speed);
  setChartData(batteryChart, history.battery);
  batteryChartStatusEl.textContent = history.batteryEverSeen
    ? ""
    : "No battery data reported by this vehicle.";
}

// --- Telemetry / Drones panel rendering ------------------------------------

const missionProgressEl = document.getElementById("mission-progress");

function renderMissionProgress(data) {
  const current = data.mission_current;
  const total = data.mission_total;

  if (!total) {
    missionProgressEl.textContent = "No active mission";
    missionProgressEl.classList.remove("active");
    highlightWaypoint(-1);
    return;
  }

  if (current >= total) {
    missionProgressEl.textContent = `Mission complete (${total}/${total})`;
  } else {
    missionProgressEl.textContent = `Waypoint ${current + 1} / ${total}`;
  }
  missionProgressEl.classList.add("active");
  highlightWaypoint(current);
}

// Updates the telemetry cards, status indicator, mission progress, and
// charts for the currently-selected drone.
function renderSelectedTelemetry(data) {
  latEl.textContent = formatNumber(data.lat, 7);
  lonEl.textContent = formatNumber(data.lon, 7);
  absAltEl.textContent = formatNumber(data.abs_alt, 2);
  relAltEl.textContent = formatNumber(data.rel_alt, 2);

  if (data.heading_deg !== null && data.heading_deg !== undefined) {
    currentHeadingDeg = data.heading_deg;
  }

  if (data.status === "connected") {
    setStatus("connected", `Connected (${DRONE_LABELS[data.drone_id]})`);
  } else {
    setStatus("waiting", `Waiting for drone (${DRONE_LABELS[data.drone_id]})`);
  }

  renderMissionProgress(data);
}

function renderDronesPanel() {
  dronesListEl.innerHTML = "";

  for (const id of DRONE_IDS) {
    const data = drones[id].latest;
    const row = document.createElement("div");
    row.className = `drone-row${id === selectedDrone ? " selected" : ""}`;
    row.addEventListener("click", () => selectDrone(id));

    const badge = document.createElement("div");
    badge.className = `drone-badge drone-color-${id}`;
    badge.textContent = DRONE_LABELS[id];

    const info = document.createElement("div");
    info.className = "drone-info";

    const top = document.createElement("div");
    top.className = "drone-info-top";

    const dot = document.createElement("span");
    dot.className = `status-dot ${data.status === "connected" ? "connected" : "waiting"}`;

    const name = document.createElement("span");
    name.textContent = DRONE_LABELS[id];

    top.appendChild(dot);
    top.appendChild(name);

    const line = document.createElement("div");
    line.className = "drone-info-line";
    if (data.status === "connected" && data.lat !== null && data.lon !== null) {
      const battery =
        data.battery_percent !== null && data.battery_percent !== undefined
          ? `${data.battery_percent.toFixed(0)}%`
          : "—";
      line.textContent = `${data.lat.toFixed(5)}, ${data.lon.toFixed(5)} · alt ${formatNumber(data.rel_alt, 1)}m · bat ${battery}`;
    } else {
      line.textContent = "Waiting for drone...";
    }

    info.appendChild(top);
    info.appendChild(line);
    row.appendChild(badge);
    row.appendChild(info);
    dronesListEl.appendChild(row);
  }
}

function selectDrone(droneId) {
  if (droneId === selectedDrone) {
    return;
  }
  selectedDrone = droneId;
  controllingDroneLabelEl.textContent = DRONE_LABELS[droneId];

  loadChartsForDrone(droneId);
  renderSelectedTelemetry(drones[droneId].latest);
  renderDronesPanel();

  const manualEnabled = drones[droneId].manualEnabled;
  manualToggleButton.textContent = `Manual: ${manualEnabled ? "ON" : "OFF"}`;
  manualToggleButton.classList.toggle("active", manualEnabled);
}

function renderTelemetry(data) {
  const state = drones[data.drone_id];
  if (!state) {
    return;
  }
  state.latest = data;

  if (data.status === "connected" && data.lat !== null && data.lon !== null) {
    updateDroneMarker(data.drone_id, data.lat, data.lon);
  }

  pushChartHistory(data.drone_id, data);
  renderDronesPanel();

  if (data.drone_id === selectedDrone) {
    renderSelectedTelemetry(data);
  }
}

function connect() {
  const socket = new WebSocket(WS_URL);

  socket.onopen = () => {
    setStatus("waiting", "Waiting for drone");
  };

  socket.onmessage = (event) => {
    renderTelemetry(JSON.parse(event.data));
  };

  socket.onclose = () => {
    setStatus("disconnected", "Disconnected — retrying");
    setTimeout(connect, RECONNECT_DELAY_MS);
  };

  socket.onerror = () => {
    socket.close();
  };
}

renderDronesPanel();
connect();

// --- Command buttons -------------------------------------------------

const commandStatusEl = document.getElementById("command-status");
const commandButtons = document.querySelectorAll("button[data-endpoint]");
const allCommandButtons = document.querySelectorAll("button[data-all-endpoint]");
const allDisableableButtons = [...commandButtons, ...allCommandButtons];

function showCommandResult(cssClass, text) {
  commandStatusEl.className = `command-status ${cssClass}`;
  commandStatusEl.textContent = text;
}

// Endpoint templates use "{drone}" as a placeholder for the currently-
// selected drone's id (e.g. "/api/{drone}/arm" -> "/api/drone1/arm").
function resolveEndpoint(endpoint) {
  return endpoint.replace("{drone}", selectedDrone);
}

async function sendCommand(endpoint, body) {
  endpoint = resolveEndpoint(endpoint);

  // Disable all command buttons while a request is in flight to avoid double-fires.
  allDisableableButtons.forEach((button) => (button.disabled = true));
  showCommandResult("", "Sending...");

  try {
    const options = { method: "POST" };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }

    const response = await fetch(endpoint, options);
    const data = await response.json();

    if (data.ok) {
      showCommandResult("ok", `${endpoint}: OK`);
    } else {
      showCommandResult("error", `${endpoint}: ${data.error}`);
    }
  } catch (err) {
    showCommandResult("error", `${endpoint}: request failed (${err.message})`);
  } finally {
    allDisableableButtons.forEach((button) => (button.disabled = false));
  }
}

// "All drones" commands fan out on the backend and return per-drone results.
async function sendAllCommand(endpoint) {
  allDisableableButtons.forEach((button) => (button.disabled = true));
  showCommandResult("", "Sending...");

  try {
    const response = await fetch(endpoint, { method: "POST" });
    const data = await response.json();

    const parts = Object.entries(data.results || {}).map(
      ([id, result]) => `${DRONE_LABELS[id] || id}: ${result.ok ? "OK" : result.error}`
    );
    showCommandResult(data.ok ? "ok" : "error", `${endpoint} — ${parts.join(", ")}`);
  } catch (err) {
    showCommandResult("error", `${endpoint}: request failed (${err.message})`);
  } finally {
    allDisableableButtons.forEach((button) => (button.disabled = false));
  }
}

commandButtons.forEach((button) => {
  if (button.id === "btn-upload-mission" || button.id === "btn-geofence-upload") {
    // Handled separately below — these need to send extra data.
    return;
  }
  button.addEventListener("click", () => sendCommand(button.dataset.endpoint));
});

allCommandButtons.forEach((button) => {
  button.addEventListener("click", () => sendAllCommand(button.dataset.allEndpoint));
});

// --- Mission planning --------------------------------------------------

const planToggleButton = document.getElementById("btn-plan-toggle");
const clearWaypointsButton = document.getElementById("btn-clear-waypoints");
const uploadMissionButton = document.getElementById("btn-upload-mission");
const altitudeInput = document.getElementById("mission-altitude");
const waypointListEl = document.getElementById("waypoint-list");
const mapEl = document.getElementById("map");

let planMode = false;

// Each entry is { lat, lon, marker }. The route polyline mirrors this order.
const waypoints = [];
const missionRoute = L.polyline([], { color: "#f59e0b", weight: 2, dashArray: "6 6" }).addTo(map);

function waypointIcon(number) {
  return L.divIcon({
    className: "waypoint-icon",
    html: String(number),
    iconSize: [24, 24],
  });
}

function highlightWaypoint(index) {
  waypoints.forEach((wp, i) => {
    const el = wp.marker.getElement();
    if (el) {
      el.classList.toggle("active", i === index);
    }
  });
}

function renderWaypointList() {
  waypointListEl.innerHTML = "";
  waypoints.forEach((wp, index) => {
    const li = document.createElement("li");
    li.textContent = `#${index + 1}: ${wp.lat.toFixed(6)}, ${wp.lon.toFixed(6)}`;
    waypointListEl.appendChild(li);
  });
}

function addWaypoint(latlng) {
  const number = waypoints.length + 1;
  const marker = L.marker(latlng, { icon: waypointIcon(number) }).addTo(map);
  waypoints.push({ lat: latlng.lat, lon: latlng.lng, marker });
  missionRoute.setLatLngs(waypoints.map((wp) => [wp.lat, wp.lon]));
  renderWaypointList();
}

function clearWaypoints() {
  waypoints.forEach((wp) => map.removeLayer(wp.marker));
  waypoints.length = 0;
  missionRoute.setLatLngs([]);
  renderWaypointList();
}

map.on("click", (event) => {
  if (planMode) {
    addWaypoint(event.latlng);
  } else if (geofenceDrawMode) {
    addGeofenceVertex(event.latlng);
  }
});

planToggleButton.addEventListener("click", () => {
  planMode = !planMode;
  planToggleButton.textContent = `Plan mission: ${planMode ? "ON" : "OFF"}`;
  planToggleButton.classList.toggle("active", planMode);

  if (planMode && geofenceDrawMode) {
    geofenceDrawMode = false;
    geofenceToggleButton.textContent = "Draw geofence: OFF";
    geofenceToggleButton.classList.remove("active");
  }

  mapEl.classList.toggle("plan-mode", planMode);
  mapEl.classList.toggle("geofence-mode", geofenceDrawMode);
});

clearWaypointsButton.addEventListener("click", clearWaypoints);

uploadMissionButton.addEventListener("click", () => {
  if (waypoints.length === 0) {
    showCommandResult("error", "mission/upload: no waypoints planned");
    return;
  }

  const body = {
    altitude: parseFloat(altitudeInput.value),
    waypoints: waypoints.map((wp) => ({ lat: wp.lat, lon: wp.lon })),
  };
  sendCommand(uploadMissionButton.dataset.endpoint, body);
});

// --- Geofence -----------------------------------------------------------

const geofenceToggleButton = document.getElementById("btn-geofence-toggle");
const geofenceClearButton = document.getElementById("btn-geofence-clear");
const geofenceUploadButton = document.getElementById("btn-geofence-upload");
const geofenceVertexCountEl = document.getElementById("geofence-vertex-count");

let geofenceDrawMode = false;

// Each entry is { lat, lon }. The polygon mirrors this order.
const geofenceVertices = [];
const geofencePolygon = L.polygon([], {
  color: "#f87171",
  weight: 2,
  fillColor: "#f87171",
  fillOpacity: 0.15,
}).addTo(map);

function renderGeofence() {
  geofencePolygon.setLatLngs(geofenceVertices.map((v) => [v.lat, v.lon]));
  geofenceVertexCountEl.textContent =
    geofenceVertices.length === 0
      ? "No geofence drawn"
      : `${geofenceVertices.length} vertex${geofenceVertices.length === 1 ? "" : "es"}`;
}

function addGeofenceVertex(latlng) {
  geofenceVertices.push({ lat: latlng.lat, lon: latlng.lng });
  renderGeofence();
}

function clearGeofenceDrawing() {
  geofenceVertices.length = 0;
  renderGeofence();
}

geofenceToggleButton.addEventListener("click", () => {
  geofenceDrawMode = !geofenceDrawMode;
  geofenceToggleButton.textContent = `Draw geofence: ${geofenceDrawMode ? "ON" : "OFF"}`;
  geofenceToggleButton.classList.toggle("active", geofenceDrawMode);

  if (geofenceDrawMode && planMode) {
    planMode = false;
    planToggleButton.textContent = "Plan mission: OFF";
    planToggleButton.classList.remove("active");
  }

  mapEl.classList.toggle("plan-mode", planMode);
  mapEl.classList.toggle("geofence-mode", geofenceDrawMode);
});

geofenceClearButton.addEventListener("click", clearGeofenceDrawing);

geofenceUploadButton.addEventListener("click", () => {
  if (geofenceVertices.length < 3) {
    showCommandResult("error", "geofence/upload: need at least 3 points");
    return;
  }

  const body = { points: geofenceVertices.map((v) => ({ lat: v.lat, lon: v.lon })) };
  sendCommand(geofenceUploadButton.dataset.endpoint, body);
});

// --- Manual control -----------------------------------------------------

const manualToggleButton = document.getElementById("btn-manual-toggle");
const velHorizontalEl = document.getElementById("vel-horizontal");
const velVerticalEl = document.getElementById("vel-vertical");

// Speed scales for keyboard taps and full joystick deflection. The joystick
// values match the backend's clamp limits so full deflection = max speed.
const KEY_FORWARD_SPEED = 5; // m/s
const KEY_RIGHT_SPEED = 5; // m/s
const KEY_VERTICAL_SPEED = 2.5; // m/s
const KEY_YAW_SPEED = 1.5; // rad/s

const MANUAL_MAX_HORIZONTAL = 8; // m/s, matches backend clamp
const MANUAL_MAX_VERTICAL = 4; // m/s, matches backend clamp
const MANUAL_MAX_YAW = 2.0; // rad/s, matches backend clamp

const MANUAL_SEND_INTERVAL_MS = 100;

const activeKeys = new Set();
let joyMove = { x: 0, y: 0 }; // x: right, y: forward
let joyVert = { x: 0, y: 0 }; // x: yaw, y: up

const KEY_AXES = {
  w: { axis: "forward", sign: 1 },
  s: { axis: "forward", sign: -1 },
  d: { axis: "right", sign: 1 },
  a: { axis: "right", sign: -1 },
  e: { axis: "yaw", sign: 1 },
  q: { axis: "yaw", sign: -1 },
  r: { axis: "up", sign: 1 },
  f: { axis: "up", sign: -1 },
};

function clampValue(value, limit) {
  return Math.max(-limit, Math.min(limit, value));
}

function computeVelocity() {
  let forward = 0;
  let right = 0;
  let up = 0;
  let yaw = 0;

  for (const key of activeKeys) {
    const mapping = KEY_AXES[key];
    if (!mapping) continue;
    if (mapping.axis === "forward") forward += mapping.sign * KEY_FORWARD_SPEED;
    if (mapping.axis === "right") right += mapping.sign * KEY_RIGHT_SPEED;
    if (mapping.axis === "up") up += mapping.sign * KEY_VERTICAL_SPEED;
    if (mapping.axis === "yaw") yaw += mapping.sign * KEY_YAW_SPEED;
  }

  // Move joystick is map-relative: "up" always means map-north and "right"
  // always means map-east, regardless of which way the drone is facing.
  // Rotate that north/east intent into the drone's forward/right body frame
  // using its current compass heading.
  const northCmd = joyMove.y * MANUAL_MAX_HORIZONTAL;
  const eastCmd = joyMove.x * MANUAL_MAX_HORIZONTAL;
  const headingRad = (currentHeadingDeg * Math.PI) / 180;
  forward += northCmd * Math.cos(headingRad) + eastCmd * Math.sin(headingRad);
  right += eastCmd * Math.cos(headingRad) - northCmd * Math.sin(headingRad);

  up += joyVert.y * MANUAL_MAX_VERTICAL;
  yaw += joyVert.x * MANUAL_MAX_YAW;

  return {
    forward: clampValue(forward, MANUAL_MAX_HORIZONTAL),
    right: clampValue(right, MANUAL_MAX_HORIZONTAL),
    up: clampValue(up, MANUAL_MAX_VERTICAL),
    yaw: clampValue(yaw, MANUAL_MAX_YAW),
  };
}

let manualSendScheduled = false;

function updateVelocity() {
  const velocity = computeVelocity();
  velHorizontalEl.textContent = `${velocity.forward.toFixed(2)} / ${velocity.right.toFixed(2)}`;
  velVerticalEl.textContent = `${velocity.up.toFixed(2)} / ${velocity.yaw.toFixed(2)}`;

  if (!drones[selectedDrone].manualEnabled || manualSendScheduled) {
    return;
  }

  manualSendScheduled = true;
  const targetDrone = selectedDrone;
  setTimeout(async () => {
    manualSendScheduled = false;
    const v = computeVelocity();
    const endpoint = `/api/${targetDrone}/manual/command`;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          forward: v.forward,
          right: v.right,
          // MAVSDK's body frame has "down" positive (descend), so the
          // joystick/keyboard "up" value must be negated here.
          down: -v.up,
          yaw_speed: v.yaw,
        }),
      });
      const data = await response.json();
      if (!data.ok) {
        showCommandResult("error", `${endpoint}: ${data.error}`);
      }
    } catch (err) {
      showCommandResult("error", `${endpoint}: request failed (${err.message})`);
    }
  }, MANUAL_SEND_INTERVAL_MS);
}

function isTypingTarget(target) {
  return target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT";
}

window.addEventListener("keydown", (event) => {
  if (!drones[selectedDrone].manualEnabled || isTypingTarget(event.target)) {
    return;
  }
  const key = event.key.toLowerCase();
  if (!KEY_AXES[key]) {
    return;
  }
  event.preventDefault();
  if (!activeKeys.has(key)) {
    activeKeys.add(key);
    updateVelocity();
  }
});

window.addEventListener("keyup", (event) => {
  const key = event.key.toLowerCase();
  if (activeKeys.has(key)) {
    activeKeys.delete(key);
    updateVelocity();
  }
});

// Hand-built draggable joystick: tracks pointer offset from the base's
// center, clamps it to the base radius, and reports a normalized -1..1
// vector via onChange. Returns a reset() to recenter the stick and zero
// its contribution (used when manual mode is turned off).
function setupJoystick(baseEl, stickEl, onChange) {
  const radius = baseEl.clientWidth / 2 - stickEl.clientWidth / 2;

  function setStick(x, y) {
    stickEl.style.transform = `translate(${(x * radius).toFixed(1)}px, ${(y * radius).toFixed(1)}px)`;
  }

  function handlePointer(event) {
    const rect = baseEl.getBoundingClientRect();
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    let dx = (event.clientX - centerX) / radius;
    let dy = (event.clientY - centerY) / radius;
    const magnitude = Math.hypot(dx, dy);
    if (magnitude > 1) {
      dx /= magnitude;
      dy /= magnitude;
    }
    setStick(dx, dy);
    // Screen Y grows downward; invert so dragging up is positive.
    onChange(dx, -dy);
  }

  function reset() {
    setStick(0, 0);
    onChange(0, 0);
  }

  stickEl.addEventListener("pointerdown", (event) => {
    stickEl.classList.add("active");
    stickEl.setPointerCapture(event.pointerId);
    handlePointer(event);
  });
  stickEl.addEventListener("pointermove", (event) => {
    if (stickEl.classList.contains("active")) {
      handlePointer(event);
    }
  });
  stickEl.addEventListener("pointerup", () => {
    stickEl.classList.remove("active");
    reset();
  });
  stickEl.addEventListener("pointercancel", () => {
    stickEl.classList.remove("active");
    reset();
  });

  return reset;
}

const resetMoveStick = setupJoystick(
  document.querySelector("#joystick-move .joystick-base"),
  document.getElementById("stick-move"),
  (x, y) => {
    // x is positive when dragging right, y is positive when dragging up —
    // matches "right" (strafe right) and "forward" directly, no inversion.
    joyMove = { x, y };
    updateVelocity();
  }
);

const resetVertStick = setupJoystick(
  document.querySelector("#joystick-vert .joystick-base"),
  document.getElementById("stick-vert"),
  (x, y) => {
    joyVert = { x, y };
    updateVelocity();
  }
);

manualToggleButton.addEventListener("click", async () => {
  const targetDrone = selectedDrone;
  const manualEnabled = drones[targetDrone].manualEnabled;
  const endpoint = `/api/${targetDrone}/manual/${manualEnabled ? "stop" : "start"}`;
  manualToggleButton.disabled = true;
  showCommandResult("", "Sending...");

  try {
    const response = await fetch(endpoint, { method: "POST" });
    const data = await response.json();

    if (data.ok) {
      drones[targetDrone].manualEnabled = !manualEnabled;
      if (targetDrone === selectedDrone) {
        manualToggleButton.textContent = `Manual: ${drones[targetDrone].manualEnabled ? "ON" : "OFF"}`;
        manualToggleButton.classList.toggle("active", drones[targetDrone].manualEnabled);
      }
      showCommandResult("ok", `${endpoint}: OK`);

      if (!drones[targetDrone].manualEnabled) {
        activeKeys.clear();
        resetMoveStick();
        resetVertStick();
        updateVelocity();
      }
    } else {
      showCommandResult("error", `${endpoint}: ${data.error}`);
    }
  } catch (err) {
    showCommandResult("error", `${endpoint}: request failed (${err.message})`);
  } finally {
    manualToggleButton.disabled = false;
  }
});
