/* RoboFetch dashboard.
 *
 * Everything on the page is driven by the /telemetry WebSocket:
 *
 *   {type:"snapshot", orders, robot, locations, items}   once, on connect
 *   {type:"order",  id, item, status, retries, detail}   whenever an order changes state
 *   {type:"pose",   x, y}                                the robot's AMCL position
 *   {type:"item",   name, x, y}                          a parcel's REAL Gazebo position
 *
 * REST is used only for the things that are actions rather than observations: submitting
 * an order, cancelling one, and reading the aggregate statistics (UC6).
 *
 * The parcels drawn on the map are Gazebo ground truth, deliberately. An order reporting
 * "completed" is a claim; the parcel's world pose is the evidence. Keeping those two
 * separate on screen is the whole lesson of HANDOVER 5.4 - the run where the system
 * reported 3/3 delivered while nothing had moved.
 */
'use strict';

/* --------------------------------------------------------------- world geometry
 * Mirrors robofetch_gazebo/worlds/warehouse.sdf. Because the map is generated from the
 * world geometry, map coordinates ARE world coordinates, so these numbers are also what
 * you see in Gazebo. Keep in sync with warehouse.sdf and scripts/generate_map.py.
 */
const WORLD = {
  bounds: { minX: -4.15, maxX: 4.15, minY: -3.15, maxY: 3.15 },
  walls: [
    { x:  0.0, y:  3.0, w: 8.1, h: 0.1 },
    { x:  0.0, y: -3.0, w: 8.1, h: 0.1 },
    { x:  4.0, y:  0.0, w: 0.1, h: 6.1 },
    { x: -4.0, y:  0.0, w: 0.1, h: 6.1 },
  ],
  shelves: [
    { name: 'shelf_1', x: -2.5, y:  1.8, w: 2.0, h: 0.5 },
    { name: 'shelf_2', x:  1.5, y:  1.8, w: 1.5, h: 0.5 },
    { name: 'shelf_3', x:  3.6, y: -1.0, w: 0.5, h: 2.0 },
  ],
  station: { x: -2.6, y: -2.0, w: 1.6, h: 1.2 },
  robotRadius: 0.22,
};

// The parcels that exist in the world. Not discovered from telemetry, because a parcel
// that never moves never publishes a pose - the dropdown would then be empty at start-up,
// which is exactly when you want to order something.
const ITEMS = ['item_1', 'item_2', 'item_3'];

// Matches each parcel's <material> in warehouse.sdf, so the map and Gazebo agree visually.
const ITEM_COLOURS = { item_1: '#e3b341', item_2: '#3388e6', item_3: '#b23fcc' };

const BUSY = ['navigating', 'grabbing', 'retrying', 'delivering', 'releasing'];

/* ------------------------------------------------------------------------ state */
const state = {
  orders: new Map(),
  locations: [],
  items: {},
  robot: null,
  trail: [],
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------- websocket */
let socket = null;
let retryDelay = 500;

function connect() {
  socket = new WebSocket(`ws://${location.host}/telemetry`);

  socket.onopen = () => {
    retryDelay = 500;
    setLink(true, 'live');
  };

  socket.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    handle(msg);
  };

  socket.onclose = () => {
    setLink(false, 'reconnecting');
    // Back off so a stopped simulation does not spin the browser at full speed.
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  };

  socket.onerror = () => socket.close();
}

function setLink(up, text) {
  const el = $('link');
  el.classList.toggle('link-up', up);
  el.classList.toggle('link-down', !up);
  $('link-text').textContent = text;
}

function handle(msg) {
  switch (msg.type) {
    case 'snapshot':
      state.locations = msg.locations || [];
      state.items = msg.items || {};
      (msg.orders || []).forEach((o) => state.orders.set(o.id, o));
      if (msg.robot && msg.robot.x != null) state.robot = { x: msg.robot.x, y: msg.robot.y };
      fillWaypoints();
      renderOrders();
      refreshStats();
      break;

    case 'order': {
      // Status messages from the robot carry only the fields that changed, so merge
      // rather than replace - otherwise point_a/point_b vanish mid-delivery.
      const existing = state.orders.get(msg.id) || {};
      state.orders.set(msg.id, { ...existing, ...msg });
      renderOrders();
      refreshStats();
      break;
    }

    case 'pose':
      state.robot = { x: msg.x, y: msg.y };
      state.trail.push([msg.x, msg.y]);
      if (state.trail.length > 400) state.trail.shift();
      break;

    case 'item':
      state.items[msg.name] = { x: msg.x, y: msg.y };
      break;
  }
  updateRobotChip();
}

/* ------------------------------------------------------------------------ form */
function fillWaypoints() {
  const names = state.locations.map((l) => l.name).sort();
  const pickups = names.filter((n) => n.startsWith('shelf'));
  const dropoffs = names.filter((n) => n.startsWith('delivery'));

  // Fall back to the full list if the registry has been renamed - the waypoint table is
  // admin-editable (UC5), so "shelf_*"/"delivery_*" is a convention, not a guarantee.
  fillSelect($('point_a'), pickups.length ? pickups : names);
  fillSelect($('point_b'), dropoffs.length ? dropoffs : names);
  fillSelect($('item'), ITEMS);
  pairItemWithShelf();
}

function fillSelect(select, values) {
  const previous = select.value;
  select.innerHTML = '';
  for (const v of values) {
    const option = document.createElement('option');
    option.value = v;
    option.textContent = v;
    select.append(option);
  }
  if (values.includes(previous)) select.value = previous;
}

// item_N lives on shelf_N, so default the route to the one that actually works. The user
// can still override it - pointing a pickup away from its parcel is how the retry FSM
// (M6) gets demonstrated.
function pairItemWithShelf() {
  const suffix = $('item').value.split('_')[1];
  const shelf = `shelf_${suffix}`;
  const delivery = `delivery_${suffix}`;
  if ([...$('point_a').options].some((o) => o.value === shelf)) $('point_a').value = shelf;
  if ([...$('point_b').options].some((o) => o.value === delivery)) $('point_b').value = delivery;
}

$('item').addEventListener('change', pairItemWithShelf);

$('order-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('submit');
  const msg = $('form-msg');
  button.disabled = true;
  msg.className = 'form-msg';
  msg.textContent = 'sending ...';

  try {
    const response = await fetch('/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item: $('item').value,
        point_a: $('point_a').value,
        point_b: $('point_b').value,
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);

    state.orders.set(body.id, body);
    renderOrders();
    msg.className = 'form-msg ok';
    // The bridge marks an order that reached no robot; surface that instead of implying
    // the delivery has started (see HANDOVER 5.8).
    msg.textContent = (body.detail || '').includes('NOT sent')
      ? `order ${body.id} stored, but NO ROBOT is listening`
      : `order ${body.id} accepted`;
  } catch (error) {
    msg.className = 'form-msg bad';
    msg.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function cancelOrder(id) {
  const msg = $('form-msg');
  try {
    const response = await fetch(`/orders/${id}`, { method: 'DELETE' });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    state.orders.set(body.id, body);
    renderOrders();
    msg.className = 'form-msg ok';
    msg.textContent = `order ${id} cancelled`;
  } catch (error) {
    msg.className = 'form-msg bad';
    msg.textContent = error.message;
  }
}

/* ---------------------------------------------------------------------- orders */
let previousStatuses = new Map();

function renderOrders() {
  const body = $('orders-body');
  const orders = [...state.orders.values()].sort((a, b) => b.id - a.id);

  if (!orders.length) {
    body.innerHTML = '<tr class="empty"><td colspan="7">No orders yet.</td></tr>';
    return;
  }

  body.innerHTML = '';
  for (const order of orders) {
    const row = document.createElement('tr');
    // Highlight only genuine changes, so the table does not flash on every pose message.
    if (previousStatuses.has(order.id) && previousStatuses.get(order.id) !== order.status) {
      row.className = 'fresh';
    }

    row.append(
      cell(String(order.id)),
      cell(order.item ?? ''),
      cell(order.point_a && order.point_b ? `${order.point_a} → ${order.point_b}` : '', 'route'),
      statusCell(order.status),
      cell(order.retries ? String(order.retries) : '–'),
      cell(order.detail || '', 'detail'),
      actionCell(order),
    );
    body.append(row);
  }

  previousStatuses = new Map(orders.map((o) => [o.id, o.status]));
}

function cell(text, className) {
  const td = document.createElement('td');
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function statusCell(status) {
  const td = document.createElement('td');
  const span = document.createElement('span');
  span.className = `status s-${status}`;
  span.textContent = status ?? 'unknown';
  td.append(span);
  return td;
}

function actionCell(order) {
  const td = document.createElement('td');
  // Only a pending order can be cancelled - the API enforces this too and returns 409.
  if (order.status === 'pending') {
    const button = document.createElement('button');
    button.className = 'cancel';
    button.type = 'button';
    button.textContent = 'Cancel';
    button.addEventListener('click', () => cancelOrder(order.id));
    td.append(button);
  }
  return td;
}

function updateRobotChip() {
  const chip = $('robot-status');
  const active = [...state.orders.values()].find((o) => BUSY.includes(o.status));
  const status = active ? active.status : 'idle';
  chip.textContent = status;
  chip.className = 'chip ' + (active ? 'chip-busy' : 'chip-idle');
}

/* ------------------------------------------------------------------ statistics */
let statsTimer = null;

// Debounced: a burst of order transitions would otherwise fire one request each.
function refreshStats() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(async () => {
    try {
      const stats = await (await fetch('/analytics')).json();
      $('stat-total').textContent = stats.total_orders ?? 0;
      $('stat-completed').textContent = stats.completed ?? 0;
      $('stat-failed').textContent = stats.failed ?? 0;
      $('stat-rate').textContent =
        stats.success_rate == null ? '–' : `${Math.round(stats.success_rate * 100)}%`;
      $('stat-avg').textContent =
        stats.average_delivery_seconds == null ? '–' : `${stats.average_delivery_seconds}s`;
    } catch { /* the panel simply keeps its last values */ }
  }, 250);
}

/* ------------------------------------------------------------------------- map */
const canvas = $('map');
const ctx = canvas.getContext('2d');
let view = { scale: 1, offsetX: 0, offsetY: 0 };

function resize() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const { minX, maxX, minY, maxY } = WORLD.bounds;
  // One scale for both axes keeps the warehouse square rather than stretched.
  view.scale = Math.min(rect.width / (maxX - minX), rect.height / (maxY - minY));
  view.offsetX = rect.width / 2;
  view.offsetY = rect.height / 2;
}

// World y points north; canvas y points down, hence the sign flip.
const tx = (x) => view.offsetX + x * view.scale;
const ty = (y) => view.offsetY - y * view.scale;

function box(b, fill, stroke) {
  const x = tx(b.x - b.w / 2);
  const y = ty(b.y + b.h / 2);
  const w = b.w * view.scale;
  const h = b.h * view.scale;
  if (fill) { ctx.fillStyle = fill; ctx.fillRect(x, y, w, h); }
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.strokeRect(x, y, w, h); }
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  const muted = css('--muted');

  // Delivery station: a floor marking, deliberately not an obstacle.
  box(WORLD.station, css('--station'), 'transparent');
  ctx.strokeStyle = '#2ea04366';
  ctx.setLineDash([4, 3]);
  box(WORLD.station, null, '#2ea04366');
  ctx.setLineDash([]);

  for (const wall of WORLD.walls) box(wall, css('--wall'));

  ctx.font = '10px system-ui, sans-serif';
  ctx.textAlign = 'center';
  for (const shelf of WORLD.shelves) {
    box(shelf, css('--shelf-fill'), css('--line'));
    ctx.fillStyle = muted;
    ctx.fillText(shelf.name.replace('shelf_', 'S'), tx(shelf.x), ty(shelf.y) + 3);
  }

  // Waypoints, straight from the database registry (UC5) rather than hard-coded.
  for (const loc of state.locations) {
    ctx.beginPath();
    ctx.arc(tx(loc.x), ty(loc.y), 3.5, 0, Math.PI * 2);
    ctx.strokeStyle = muted;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.fillStyle = muted;
    ctx.fillText(loc.name.replace('delivery_', 'D').replace('shelf_', 'P'),
                 tx(loc.x), ty(loc.y) - 7);
  }

  // Parcels: live Gazebo poses. A 0.09 m cube is sub-pixel at this scale, so it is drawn
  // at a readable minimum size - position is truthful, size is not.
  for (const [name, pose] of Object.entries(state.items)) {
    const size = Math.max(7, 0.09 * view.scale);
    ctx.fillStyle = ITEM_COLOURS[name] || '#e3b341';
    ctx.fillRect(tx(pose.x) - size / 2, ty(pose.y) - size / 2, size, size);
    ctx.fillStyle = muted;
    ctx.fillText(name.replace('item_', ''), tx(pose.x), ty(pose.y) - size);
  }

  if (state.robot) {
    if (state.trail.length > 1) {
      ctx.beginPath();
      ctx.moveTo(tx(state.trail[0][0]), ty(state.trail[0][1]));
      for (const [x, y] of state.trail.slice(1)) ctx.lineTo(tx(x), ty(y));
      ctx.strokeStyle = '#4b9fff55';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    const r = Math.max(5, WORLD.robotRadius * view.scale);
    ctx.beginPath();
    ctx.arc(tx(state.robot.x), ty(state.robot.y), r, 0, Math.PI * 2);
    ctx.fillStyle = '#4b9fff';
    ctx.fill();
    ctx.strokeStyle = '#ffffffaa';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  requestAnimationFrame(draw);
}

new ResizeObserver(resize).observe(canvas);
resize();
requestAnimationFrame(draw);
connect();
