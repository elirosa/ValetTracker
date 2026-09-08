/* ValetTracker client. No framework — the whole surface is one list and one form. */

const $ = (sel) => document.querySelector(sel);

const state = {
  view: 'all',
  q: '',
  tickets: [],
  role: 'valet',
  clockSkew: 0,      // server epoch minus browser epoch
  editingId: null,
  nextNo: '',
};

const COLORS = [
  ['Black', '#1a1a1a'], ['White', '#f4f4f2'], ['Silver', '#b9bec4'],
  ['Gray', '#7c848c'], ['Blue', '#2d5bb9'], ['Red', '#b52f2b'],
];

/* --- helpers ------------------------------------------------------------ */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) {
    showGate();
    throw new Error('Signed out');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

function elapsed(since) {
  const secs = Math.max(0, Math.floor(Date.now() / 1000 + state.clockSkew) - since);
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  return mins % 60 ? `${hrs}h ${mins % 60}m` : `${hrs}h`;
}

function escapeHtml(text) {
  return String(text || '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* --- sign in ------------------------------------------------------------ */

let pin = '';

function renderPin() {
  $('#pinDisplay').innerHTML = '<span class="pin-dot"></span>'.repeat(pin.length);
}

function showGate() {
  pin = '';
  renderPin();
  $('#gate').hidden = false;
  $('#app').hidden = true;
}

async function submitPin() {
  if (!pin) return;
  try {
    const data = await api('/api/session', {
      method: 'POST',
      body: JSON.stringify({ pin }),
    });
    pin = '';
    renderPin();
    $('#gateMsg').textContent = '';
    startApp(data);
  } catch (err) {
    pin = '';
    renderPin();
    $('#gateMsg').textContent = err.message;
  }
}

$('.keypad').addEventListener('click', (event) => {
  const key = event.target.closest('.key');
  if (!key) return;
  const value = key.dataset.key;
  if (value === 'clear') pin = '';
  else if (value === 'enter') return submitPin();
  else if (pin.length < 12) pin += value;
  renderPin();
});

document.addEventListener('keydown', (event) => {
  if ($('#gate').hidden) return;
  if (/^[0-9]$/.test(event.key) && pin.length < 12) { pin += event.key; renderPin(); }
  else if (event.key === 'Backspace') { pin = pin.slice(0, -1); renderPin(); }
  else if (event.key === 'Enter') submitPin();
});

/* --- rendering ---------------------------------------------------------- */

function statusLine(ticket) {
  if (ticket.status === 'requested') {
    return `<span class="state waiting">Guest waiting ${elapsed(ticket.requested_at)}</span>`;
  }
  if (ticket.status === 'ready') {
    return `<span class="state out">Out front ${elapsed(ticket.ready_at)}</span>`;
  }
  return '';
}

function actionsFor(ticket) {
  const id = ticket.id;
  if (ticket.status === 'parked') {
    return `<button class="act act-main" data-go="requested" data-id="${id}">Guest is here</button>
            <button class="act act-icon" data-edit="${id}">Edit</button>`;
  }
  if (ticket.status === 'requested') {
    return `<button class="act act-main" data-go="ready" data-id="${id}">Car is out front</button>
            <button class="act act-go" data-go="delivered" data-id="${id}">Handed over</button>
            <button class="act act-icon" data-go="parked" data-id="${id}">Undo</button>`;
  }
  if (ticket.status === 'ready') {
    return `<button class="act act-go" data-go="delivered" data-id="${id}">Handed over</button>
            <button class="act act-icon" data-go="requested" data-id="${id}">Undo</button>`;
  }
  const remove = state.role === 'admin'
    ? `<button class="act act-icon" data-del="${id}">Delete</button>` : '';
  return `<button class="act act-icon" data-go="parked" data-id="${id}">Back to lot</button>${remove}`;
}

function stubHtml(ticket) {
  const car = [ticket.color, ticket.make, ticket.model].filter(Boolean).join(' ') || 'Vehicle';
  const meta = [];
  if (ticket.plate) meta.push(escapeHtml(ticket.plate));
  if (ticket.spot) meta.push(`<span class="spot">Spot ${escapeHtml(ticket.spot)}</span>`);
  if (ticket.phone) meta.push(escapeHtml(ticket.phone));

  const clock = ticket.status === 'delivered'
    ? 'closed'
    : `${elapsed(ticket.created_at)} on lot`;

  return `<article class="stub is-${ticket.status}">
    <div class="stub-no">
      <b>${escapeHtml(ticket.ticket_no)}</b>
      <span class="stub-clock">${clock}</span>
    </div>
    <div class="stub-body">
      <h3 class="stub-car">${escapeHtml(car)}</h3>
      <p class="stub-meta">${meta.join(' &middot; ') || 'No details recorded'}</p>
      ${ticket.notes ? `<p class="stub-note">${escapeHtml(ticket.notes)}</p>` : ''}
      ${statusLine(ticket)}
      <div class="stub-actions">${actionsFor(ticket)}</div>
    </div>
  </article>`;
}

function visibleTickets() {
  if (state.view === 'requested') return state.tickets.filter((t) => t.status === 'requested');
  if (state.view === 'ready') return state.tickets.filter((t) => t.status === 'ready');
  return state.tickets;
}

function emptyMessage() {
  if (state.q) return `Nothing matches “${state.q}”.`;
  if (state.view === 'requested') return 'Nobody is waiting on a car right now.';
  if (state.view === 'ready') return 'No cars are staged out front.';
  if (state.view === 'history') return 'No cars have been handed back yet.';
  return 'The lot is empty. Park a car to open the first ticket.';
}

function render() {
  const rows = visibleTickets();
  $('#list').innerHTML = rows.map(stubHtml).join('');
  $('#empty').hidden = rows.length > 0;
  $('#empty').textContent = emptyMessage();
}

function renderStats(stats) {
  $('#cAll').textContent = stats.on_lot + stats.requested + stats.ready;
  $('#cReq').textContent = stats.requested;
  $('#cReady').textContent = stats.ready;

  const parts = [];
  if (stats.delivered_24h) parts.push(`${stats.delivered_24h} cars returned in the last 24 hours`);
  if (stats.avg_retrieval_seconds != null) {
    parts.push(`averaging ${Math.round(stats.avg_retrieval_seconds / 60)} min from request to handover`);
  }
  $('#pace').textContent = parts.join(', ');
}

/* --- data --------------------------------------------------------------- */

let refreshTimer;

async function refresh() {
  const scope = state.view === 'history' ? 'closed' : 'open';
  const url = `/api/tickets?scope=${scope}&q=${encodeURIComponent(state.q)}`;
  try {
    const data = await api(url);
    state.tickets = data.tickets;
    state.nextNo = data.next_ticket_no;
    state.clockSkew = data.server_time - Math.floor(Date.now() / 1000);
    renderStats(data.stats);
    render();
  } catch (err) {
    if (err.message !== 'Signed out') toast(err.message, true);
  }
}

function scheduleRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refresh, 15000);
  // Keep the on-lot timers honest between polls.
  setInterval(render, 30000);
}

/* --- actions ------------------------------------------------------------ */

$('#list').addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;

  if (button.dataset.go) {
    button.disabled = true;
    try {
      await api(`/api/tickets/${button.dataset.id}/status`, {
        method: 'POST',
        body: JSON.stringify({ status: button.dataset.go }),
      });
      await refresh();
    } catch (err) {
      toast(err.message, true);
      button.disabled = false;
    }
    return;
  }

  if (button.dataset.edit) {
    const ticket = state.tickets.find((t) => t.id === Number(button.dataset.edit));
    if (ticket) openSheet(ticket);
    return;
  }

  if (button.dataset.del) {
    if (!confirm('Delete this ticket and its history for good?')) return;
    try {
      await api(`/api/tickets/${button.dataset.del}`, { method: 'DELETE' });
      toast('Ticket deleted.');
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  }
});

/* --- sheet -------------------------------------------------------------- */

function openSheet(ticket) {
  const form = $('#ticketForm');
  form.reset();
  $('#formMsg').textContent = '';
  state.editingId = ticket ? ticket.id : null;

  $('#sheetTitle').textContent = ticket ? `Ticket ${ticket.ticket_no}` : 'Park a car';
  $('#submitBtn').textContent = ticket ? 'Save changes' : 'Park it';

  if (ticket) {
    for (const key of ['ticket_no', 'plate', 'make', 'model', 'color', 'spot', 'phone', 'notes']) {
      if (form.elements[key]) form.elements[key].value = ticket[key] || '';
    }
  } else {
    form.elements.ticket_no.value = state.nextNo || '';
  }

  markSwatch();
  $('#sheetBackdrop').hidden = false;
  setTimeout(() => form.elements.plate.focus(), 60);
}

function closeSheet() {
  $('#sheetBackdrop').hidden = true;
  state.editingId = null;
}

$('#openNew').addEventListener('click', () => openSheet(null));
$('#closeSheet').addEventListener('click', closeSheet);
$('#sheetBackdrop').addEventListener('click', (event) => {
  if (event.target === $('#sheetBackdrop')) closeSheet();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('#sheetBackdrop').hidden) closeSheet();
});

$('#swatches').innerHTML = COLORS.map(([name, hex]) =>
  `<button type="button" class="swatch" data-color="${name}"><i style="background:${hex}"></i>${name}</button>`
).join('');

function markSwatch() {
  const current = $('#colorInput').value.trim().toLowerCase();
  document.querySelectorAll('.swatch').forEach((el) => {
    el.classList.toggle('is-on', el.dataset.color.toLowerCase() === current);
  });
}

$('#swatches').addEventListener('click', (event) => {
  const swatch = event.target.closest('.swatch');
  if (!swatch) return;
  const input = $('#colorInput');
  input.value = input.value.trim().toLowerCase() === swatch.dataset.color.toLowerCase()
    ? '' : swatch.dataset.color;
  markSwatch();
});
$('#colorInput').addEventListener('input', markSwatch);

$('#ticketForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const payload = {};
  for (const key of ['ticket_no', 'plate', 'make', 'model', 'color', 'spot', 'phone', 'notes']) {
    payload[key] = form.elements[key].value.trim();
  }

  const button = $('#submitBtn');
  button.disabled = true;
  $('#formMsg').textContent = '';

  try {
    if (state.editingId) {
      await api(`/api/tickets/${state.editingId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      toast(`Ticket ${payload.ticket_no} updated.`);
    } else {
      const data = await api('/api/tickets', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      toast(`Ticket ${data.ticket.ticket_no} parked.`);
    }
    closeSheet();
    await refresh();
  } catch (err) {
    $('#formMsg').textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

/* --- filters, search, session ------------------------------------------- */

$('#filters').addEventListener('click', (event) => {
  const chip = event.target.closest('.chip');
  if (!chip) return;
  document.querySelectorAll('.chip').forEach((c) => c.classList.toggle('is-on', c === chip));
  state.view = chip.dataset.view;
  refresh();
});

let searchTimer;
$('#search').addEventListener('input', (event) => {
  state.q = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refresh, 220);
});

$('#signOut').addEventListener('click', async () => {
  await fetch('/api/session', { method: 'DELETE' });
  clearInterval(refreshTimer);
  showGate();
});

function startApp(session) {
  state.role = session.role || 'valet';
  $('#lotName').textContent = session.lot || '';
  $('#gate').hidden = true;
  $('#app').hidden = false;
  refresh();
  scheduleRefresh();
}

(async function boot() {
  try {
    const session = await fetch('/api/session').then((r) => r.json());
    $('#gateLot').textContent = session.lot || '';
    if (session.authenticated) startApp(session);
    else showGate();
  } catch {
    showGate();
  }
})();
