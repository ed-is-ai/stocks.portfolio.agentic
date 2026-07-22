// Advanced watchlist controller: search, toggle filters, numeric range filters,
// multi-column sort, column show/hide and saved views. State lives in this module
// scope (outside the HTMX-swapped content) and is persisted to localStorage so it
// survives a /refresh-data swap and a full page reload.
(function () {
  'use strict';

  const STATE_KEY = 'wl-state-v1';
  const PRESET_KEY = 'wl-presets-v1';

  // Columns that participate in range filtering, with their valid bounds.
  const RANGE_FIELDS = [
    { col: 'score', label: 'Score', min: 1, max: 10 },
    { col: 'canslim', label: 'CANSLIM', min: 0, max: 14 },
    { col: 'momentum', label: 'Momentum', min: 0, max: 14 },
    { col: 'rsi', label: 'RSI', min: 0, max: 100 },
    { col: 'risk', label: 'Risk %', min: 0, max: 100 },
    { col: 'vsentry', label: 'vs Entry %', min: -50, max: 50 },
    { col: 'sepa', label: 'SEPA', min: 0, max: 8 },
  ];

  // Every column in table order. `locked` columns can never be hidden.
  const COLUMNS = [
    { col: 'ticker', label: 'Ticker', locked: true },
    { col: 'rec', label: 'Rec' },
    { col: 'score', label: 'Score' },
    { col: 'canslim', label: 'CANSLIM' },
    { col: 'momentum', label: 'Momentum' },
    { col: 'stage', label: 'Stage' },
    { col: 'sector', label: 'Sector' },
    { col: 'price', label: 'Price' },
    { col: 'entry', label: 'Entry' },
    { col: 'vsentry', label: 'vs Entry' },
    { col: 'stop', label: 'Stop' },
    { col: 'vsstop', label: 'vs Stop' },
    { col: 'risk', label: 'Risk %' },
    { col: 'targets', label: 'Targets' },
    { col: 'rsi', label: 'RSI' },
    { col: 'sepa', label: 'SEPA' },
    { col: 'buy', label: 'Action', locked: true },
  ];

  // Sensible default visible set: hide the denser secondary metrics up front.
  const DEFAULT_HIDDEN = ['canslim', 'momentum', 'vsstop'];

  // Default sort direction per column when a header is first clicked.
  const DEFAULT_DIR = {
    ticker: 'asc', sector: 'asc', entry: 'asc', stop: 'asc',
    risk: 'asc', vsentry: 'asc', vsstop: 'desc',
  };
  const dirFor = (col) => DEFAULT_DIR[col] || 'desc';

  const TOGGLE_FILTERS = {
    sepa8: (row) => row.dataset.sepa === '8',
    alert: (row) => row.dataset.rec === 'alert',
    breakout: (row) => row.dataset.breakout === 'true',
    myb: (row) => row.dataset.myb === 'true',
    uk: (row) => row.dataset.market === 'uk',
  };

  const BUILTIN_PRESETS = [
    {
      id: 'builtin-buy-ready', name: 'Buy-ready', builtin: true,
      state: {
        filters: [], ranges: { score: [7, 10], sepa: [6, 8] },
        sortKeys: [{ field: 'score', dir: 'desc' }], hidden: DEFAULT_HIDDEN.slice(),
      },
    },
    {
      id: 'builtin-uk-breakouts', name: 'UK breakouts', builtin: true,
      state: {
        filters: ['uk', 'breakout'], ranges: {},
        sortKeys: [{ field: 'score', dir: 'desc' }], hidden: DEFAULT_HIDDEN.slice(),
      },
    },
    {
      id: 'builtin-low-risk-stage2', name: 'Low-risk Stage 2', builtin: true,
      state: {
        filters: [], ranges: { risk: [null, 8], score: [5, 10] },
        sortKeys: [{ field: 'risk', dir: 'asc' }],
        hidden: DEFAULT_HIDDEN.filter((c) => c !== 'vsstop'),
      },
    },
  ];

  const defaultState = () => ({
    query: '',
    filters: [],
    ranges: {},
    sortKeys: [],
    hidden: DEFAULT_HIDDEN.slice(),
    presetId: null,
  });

  let state = loadState();
  let originalRows = []; // server-provided (actionable-first) order

  // ── Persistence ────────────────────────────────────────────────
  function loadState() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (raw) return Object.assign(defaultState(), JSON.parse(raw));
    } catch (e) { /* ignore corrupt state */ }
    return defaultState();
  }

  function saveState() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch (e) { /* quota */ }
  }

  function loadPresets() {
    let user = [];
    try {
      const raw = localStorage.getItem(PRESET_KEY);
      if (raw) user = JSON.parse(raw).filter((p) => p && !p.builtin);
    } catch (e) { /* ignore */ }
    return BUILTIN_PRESETS.concat(user);
  }

  function saveUserPresets(presets) {
    const user = presets.filter((p) => !p.builtin);
    try { localStorage.setItem(PRESET_KEY, JSON.stringify(user)); } catch (e) { /* quota */ }
  }

  // ── DOM helpers ────────────────────────────────────────────────
  const table = () => document.getElementById('watchlist-table');
  const tbody = () => { const t = table(); return t ? t.querySelector('tbody') : null; };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  // Canonical value for a row/column, read from the cell's data-val.
  // Returns a number, a non-empty string, or null when the value is unavailable.
  function cellVal(row, col) {
    const cell = row.querySelector('[data-col="' + col + '"]');
    if (!cell) return null;
    const raw = cell.dataset.val;
    if (raw === undefined || raw === '') return null;
    const num = Number(raw);
    return Number.isNaN(num) ? raw : num;
  }

  // ── Column visibility ──────────────────────────────────────────
  function applyColumns() {
    const hidden = new Set(state.hidden);
    COLUMNS.forEach(({ col }) => {
      const on = hidden.has(col);
      document.querySelectorAll('#watchlist-table [data-col="' + col + '"]').forEach((cell) => {
        cell.classList.toggle('wl-hidden', on);
      });
    });
  }

  // ── Sorting ────────────────────────────────────────────────────
  function compareRows(a, b, key) {
    const va = cellVal(a, key.field);
    const vb = cellVal(b, key.field);
    const ma = va === null, mb = vb === null;
    if (ma && mb) return 0;
    if (ma) return 1; // unavailable values always sort last
    if (mb) return -1;
    let cmp;
    if (typeof va === 'number' && typeof vb === 'number') cmp = va - vb;
    else cmp = String(va).localeCompare(String(vb));
    return key.dir === 'asc' ? cmp : -cmp;
  }

  function applySort() {
    const body = tbody();
    if (!body) return;
    let rows = originalRows.slice();
    if (state.sortKeys.length) {
      const baseIndex = new Map(originalRows.map((r, i) => [r, i]));
      rows.sort((a, b) => {
        for (const key of state.sortKeys) {
          const cmp = compareRows(a, b, key);
          if (cmp !== 0) return cmp;
        }
        return baseIndex.get(a) - baseIndex.get(b); // stable tiebreak
      });
    }
    rows.forEach((r) => body.appendChild(r));
    updateSortIndicators();
  }

  function updateSortIndicators() {
    document.querySelectorAll('#watchlist-table .wl-sort').forEach((btn) => {
      const idx = state.sortKeys.findIndex((k) => k.field === btn.dataset.col);
      const ind = btn.querySelector('.wl-sort-ind');
      const th = btn.closest('th');
      if (idx >= 0) {
        const key = state.sortKeys[idx];
        const arrow = key.dir === 'asc' ? '▲' : '▼';
        ind.textContent = arrow + (state.sortKeys.length > 1 ? String(idx + 1) : '');
        btn.classList.add('sorted');
        if (th) th.setAttribute('aria-sort', key.dir === 'asc' ? 'ascending' : 'descending');
      } else {
        ind.textContent = '';
        btn.classList.remove('sorted');
        if (th) th.removeAttribute('aria-sort');
      }
    });
  }

  function nextDir(col, cur) {
    const base = dirFor(col);
    if (!cur) return base;
    if (cur === base) return base === 'asc' ? 'desc' : 'asc';
    return null; // third activation clears the key
  }

  function handleSort(col, shift) {
    const idx = state.sortKeys.findIndex((k) => k.field === col);
    const cur = idx >= 0 ? state.sortKeys[idx].dir : null;
    const dir = nextDir(col, cur);
    if (!shift) state.sortKeys = []; // plain click resets to a single key
    const at = state.sortKeys.findIndex((k) => k.field === col);
    if (dir === null) {
      if (at >= 0) state.sortKeys.splice(at, 1);
    } else if (at >= 0) {
      state.sortKeys[at].dir = dir;
    } else {
      state.sortKeys.push({ field: col, dir: dir });
    }
    state.presetId = null;
    apply();
  }

  // ── Filtering (toggles + query + ranges) ───────────────────────
  function applyFilters() {
    const q = state.query.trim().toLowerCase();
    let shown = 0;
    originalRows.forEach((row) => {
      let ok = true;
      for (const f of state.filters) {
        const test = TOGGLE_FILTERS[f];
        if (test && !test(row)) { ok = false; break; }
      }
      if (ok && q) {
        const ticker = String(cellVal(row, 'ticker') || '');
        const sector = String(cellVal(row, 'sector') || '');
        if (!(ticker + ' ' + sector).toLowerCase().includes(q)) ok = false;
      }
      if (ok) {
        for (const f of RANGE_FIELDS) {
          const rng = state.ranges[f.col];
          if (!rng) continue;
          const min = rng[0], max = rng[1];
          if (min == null && max == null) continue;
          const v = cellVal(row, f.col);
          if (typeof v !== 'number') { ok = false; break; }
          if (min != null && v < min) { ok = false; break; }
          if (max != null && v > max) { ok = false; break; }
        }
      }
      row.style.display = ok ? '' : 'none';
      if (ok) shown += 1;
    });
    const counter = document.getElementById('wl-match-count');
    if (counter) counter.textContent = String(shown);
  }

  // ── Apply everything + persist ─────────────────────────────────
  function apply() {
    applyColumns();
    applySort();
    applyFilters();
    saveState();
  }

  // ── Toolbar controls ───────────────────────────────────────────
  function closeDropdowns(except) {
    document.querySelectorAll('#wl-adv .wl-dd.open').forEach((dd) => {
      if (dd !== except) dd.classList.remove('open');
    });
  }

  function dropdown(label, icon, panel) {
    const btn = el('button', {
      class: 'btn-filter wl-dd-toggle', type: 'button', 'aria-haspopup': 'true', 'aria-expanded': 'false',
      html: '<i class="bi bi-' + icon + '"></i> ' + label,
    });
    const dd = el('div', { class: 'wl-dd' }, [btn, panel]);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = dd.classList.contains('open');
      closeDropdowns(dd);
      dd.classList.toggle('open', !open);
      btn.setAttribute('aria-expanded', String(!open));
    });
    panel.addEventListener('click', (e) => e.stopPropagation());
    return dd;
  }

  function buildRangesPanel() {
    const rows = RANGE_FIELDS.map((f) => {
      const rng = state.ranges[f.col] || [null, null];
      const mkInput = (which, value) => el('input', {
        type: 'number', class: 'wl-range-input', step: 'any',
        min: String(f.min), max: String(f.max), placeholder: which === 0 ? 'min' : 'max',
        'aria-label': f.label + ' ' + (which === 0 ? 'minimum' : 'maximum'),
        value: value == null ? '' : String(value),
        oninput: (e) => {
          const raw = e.target.value.trim();
          const num = raw === '' ? null : Number(raw);
          const cur = state.ranges[f.col] || [null, null];
          cur[which] = (raw === '' || Number.isNaN(num)) ? null : num;
          if (cur[0] == null && cur[1] == null) delete state.ranges[f.col];
          else state.ranges[f.col] = cur;
          state.presetId = null;
          apply();
        },
      });
      return el('div', { class: 'wl-range-row' }, [
        el('span', { class: 'wl-range-label', text: f.label }),
        mkInput(0, rng[0]),
        el('span', { class: 'wl-range-dash', text: '–' }),
        mkInput(1, rng[1]),
      ]);
    });
    const clear = el('button', {
      class: 'wl-panel-action', type: 'button', text: 'Clear all ranges',
      onclick: () => { state.ranges = {}; state.presetId = null; render(); },
    });
    return el('div', { class: 'wl-dd-panel wl-ranges-panel' }, rows.concat([clear]));
  }

  function buildColumnsPanel() {
    const hidden = new Set(state.hidden);
    const items = COLUMNS.filter((c) => !c.locked).map((c) => {
      const cb = el('input', {
        type: 'checkbox', class: 'wl-col-check',
        checked: hidden.has(c.col) ? null : 'checked',
        onchange: (e) => {
          if (e.target.checked) state.hidden = state.hidden.filter((x) => x !== c.col);
          else if (!state.hidden.includes(c.col)) state.hidden.push(c.col);
          state.presetId = null;
          apply();
        },
      });
      if (!hidden.has(c.col)) cb.checked = true;
      return el('label', { class: 'wl-col-item' }, [cb, el('span', { text: c.label })]);
    });
    const reset = el('button', {
      class: 'wl-panel-action', type: 'button', text: 'Reset to default columns',
      onclick: () => { state.hidden = DEFAULT_HIDDEN.slice(); state.presetId = null; render(); },
    });
    return el('div', { class: 'wl-dd-panel wl-columns-panel' }, items.concat([reset]));
  }

  function buildViewsPanel() {
    const presets = loadPresets();
    const list = el('div', { class: 'wl-views-list' }, presets.map((p) => {
      const applyBtn = el('button', {
        class: 'wl-view-apply' + (state.presetId === p.id ? ' active' : ''),
        type: 'button', text: p.name,
        onclick: () => applyPreset(p.id),
      });
      const children = [applyBtn];
      if (!p.builtin) {
        children.push(el('button', {
          class: 'wl-view-del', type: 'button', title: 'Delete view', 'aria-label': 'Delete view ' + p.name,
          html: '<i class="bi bi-trash"></i>',
          onclick: () => deletePreset(p.id),
        }));
      }
      return el('div', { class: 'wl-view-row' }, children);
    }));
    const nameInput = el('input', {
      type: 'text', class: 'wl-view-name', placeholder: 'Name this view', 'aria-label': 'New view name',
    });
    const saveBtn = el('button', {
      class: 'wl-panel-action', type: 'button', text: 'Save current view',
      onclick: () => { savePreset(nameInput.value); },
    });
    return el('div', { class: 'wl-dd-panel wl-views-panel' }, [
      list,
      el('div', { class: 'wl-view-save' }, [nameInput, saveBtn]),
    ]);
  }

  function buildAdv() {
    const adv = document.getElementById('wl-adv');
    if (!adv) return;
    adv.innerHTML = '';
    adv.appendChild(dropdown('Filters', 'sliders', buildRangesPanel()));
    adv.appendChild(dropdown('Columns', 'layout-three-columns', buildColumnsPanel()));
    adv.appendChild(dropdown('Views', 'bookmark-star', buildViewsPanel()));
    adv.appendChild(el('button', {
      class: 'btn-filter wl-reset', type: 'button',
      html: '<i class="bi bi-arrow-counterclockwise"></i> Recommended order',
      onclick: () => { state.sortKeys = []; state.presetId = null; apply(); },
    }));
  }

  // Sync the server-rendered controls (search box, toggle buttons) to state.
  function syncStatic() {
    const search = document.getElementById('wl-search');
    if (search) search.value = state.query;
    document.querySelectorAll('#wl-toolbar [data-filter]').forEach((btn) => {
      const on = state.filters.includes(btn.dataset.filter);
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', String(on));
    });
  }

  // ── Presets ────────────────────────────────────────────────────
  function applyPreset(id) {
    const preset = loadPresets().find((p) => p.id === id);
    if (!preset) return;
    state = Object.assign(defaultState(), JSON.parse(JSON.stringify(preset.state)));
    state.presetId = id;
    render();
  }

  function savePreset(name) {
    const trimmed = (name || '').trim();
    if (!trimmed) return;
    const presets = loadPresets();
    const id = 'user-' + trimmed.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    const snapshot = {
      query: state.query, filters: state.filters.slice(),
      ranges: JSON.parse(JSON.stringify(state.ranges)),
      sortKeys: JSON.parse(JSON.stringify(state.sortKeys)),
      hidden: state.hidden.slice(),
    };
    const existing = presets.findIndex((p) => p.id === id && !p.builtin);
    const entry = { id: id, name: trimmed, state: snapshot };
    if (existing >= 0) presets[existing] = entry;
    else presets.push(entry);
    saveUserPresets(presets);
    state.presetId = id;
    saveState();
    render();
  }

  function deletePreset(id) {
    const presets = loadPresets().filter((p) => p.id !== id);
    saveUserPresets(presets);
    if (state.presetId === id) state.presetId = null;
    render();
  }

  // ── Render / init ──────────────────────────────────────────────
  function render() {
    buildAdv();
    syncStatic();
    apply();
  }

  function init() {
    const body = tbody();
    if (!body) return; // not the watchlist partial
    originalRows = Array.prototype.slice.call(body.rows);
    render();
  }

  // ── Global event wiring (bound once) ───────────────────────────
  document.body.addEventListener('click', (event) => {
    const toggle = event.target.closest('#wl-toolbar [data-filter]');
    if (toggle) {
      const filter = toggle.dataset.filter;
      if (state.filters.includes(filter)) state.filters = state.filters.filter((f) => f !== filter);
      else state.filters.push(filter);
      const on = state.filters.includes(filter);
      toggle.classList.toggle('active', on);
      toggle.setAttribute('aria-pressed', String(on));
      state.presetId = null;
      apply();
      return;
    }
    const sortBtn = event.target.closest('#watchlist-table .wl-sort');
    if (sortBtn) {
      handleSort(sortBtn.dataset.col, event.shiftKey);
      return;
    }
    if (!event.target.closest('#wl-adv .wl-dd')) closeDropdowns(null);
  });

  document.body.addEventListener('input', (event) => {
    if (event.target.id === 'wl-search') {
      state.query = event.target.value;
      state.presetId = null;
      apply();
    }
  });

  document.body.addEventListener('htmx:afterSettle', (event) => {
    if (event.detail.target && event.detail.target.id === 'tab-content') init();
  });

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();

function renderLocalTimes(root = document) {
  root.querySelectorAll("time.local-time[datetime]").forEach((element) => {
    const instant = new Date(element.dateTime);
    if (Number.isNaN(instant.getTime())) return;
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    const zoneName = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
      .formatToParts(instant)
      .find((part) => part.type === "timeZoneName")?.value || zone;
    element.textContent = `${instant.toLocaleString()} ${zoneName}`;
    element.title = `${element.dateTime} UTC source; displayed in ${zone}`;
  });
}

document.addEventListener("DOMContentLoaded", () => renderLocalTimes());
document.body.addEventListener("htmx:afterSwap", (event) => renderLocalTimes(event.target));
