// Realised P&L round-trip table controller: search, multi-column sort and
// column show/hide. Mirrors watchlist.js conventions (data-col/data-val
// cells, .wl-sort headers, cellVal/compareRows) but is scoped to #pnl-table
// with its own localStorage key. State lives in module scope so it survives
// an htmx tab swap and a full reload.
(function () {
  'use strict';

  const STATE_KEY = 'pnl-table-state-v1';

  // Every column in table order. `locked` columns can never be hidden.
  const COLUMNS = [
    { col: 'ticker', label: 'Ticker', locked: true },
    { col: 'entered', label: 'Entered' },
    { col: 'exited', label: 'Exited' },
    { col: 'held', label: 'Held' },
    { col: 'stake', label: 'Stake' },
    { col: 'buyprice', label: 'Buy price' },
    { col: 'sellprice', label: 'Sell price' },
    { col: 'result', label: 'Result' },
    { col: 'pnlpct', label: 'P&L %' },
  ];

  // Buy/Sell price are reference detail, hidden until asked for.
  const DEFAULT_HIDDEN = ['buyprice', 'sellprice'];

  const DEFAULT_DIR = { ticker: 'asc', entered: 'asc', exited: 'asc', held: 'asc' };
  const dirFor = (col) => DEFAULT_DIR[col] || 'desc';

  const defaultState = () => ({
    query: '',
    sortKeys: [],
    hidden: DEFAULT_HIDDEN.slice(),
  });

  let state = loadState();
  let originalRows = []; // server-provided (newest-exit-first) order

  // Guard against valid JSON of the wrong shape (hand-edited storage, an old
  // schema): a non-array sortKeys/hidden would throw on the first .filter/Set.
  function loadState() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (raw) {
        const p = JSON.parse(raw);
        return {
          query: typeof p.query === 'string' ? p.query : '',
          sortKeys: Array.isArray(p.sortKeys)
            ? p.sortKeys.filter(
                (k) => k && typeof k.field === 'string'
                  && (k.dir === 'asc' || k.dir === 'desc'),
              )
            : [],
          hidden: Array.isArray(p.hidden) ? p.hidden : DEFAULT_HIDDEN.slice(),
        };
      }
    } catch (e) { /* ignore corrupt state */ }
    return defaultState();
  }

  function saveState() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch (e) { /* quota */ }
  }

  const table = () => document.getElementById('pnl-table');
  const tbody = () => { const t = table(); return t ? t.querySelector('tbody') : null; };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') {
        node.addEventListener(k.slice(2), v);
      } else if (v !== null && v !== undefined) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  // Canonical value for a row/column, read from the cell's data-val.
  // Returns a number, a non-empty string, or null when unavailable.
  function cellVal(row, col) {
    const cell = row.querySelector('[data-col="' + col + '"]');
    if (!cell) return null;
    const raw = cell.dataset.val;
    if (raw === undefined || raw === '') return null;
    const num = Number(raw);
    return Number.isNaN(num) ? raw : num;
  }

  function applyColumns() {
    const hidden = new Set(state.hidden);
    COLUMNS.forEach(({ col }) => {
      const on = hidden.has(col);
      document.querySelectorAll('#pnl-table [data-col="' + col + '"]').forEach((cell) => {
        cell.classList.toggle('wl-hidden', on);
      });
    });
  }

  function compareRows(a, b, key) {
    const va = cellVal(a, key.field);
    const vb = cellVal(b, key.field);
    const ma = va === null;
    const mb = vb === null;
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
    const rows = originalRows.slice();
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
    document.querySelectorAll('#pnl-table .wl-sort').forEach((btn) => {
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
    apply();
  }

  function applyFilters() {
    const q = state.query.trim().toLowerCase();
    let shown = 0;
    originalRows.forEach((row) => {
      const ticker = String(cellVal(row, 'ticker') || '').toLowerCase();
      const ok = !q || ticker.includes(q);
      row.style.display = ok ? '' : 'none';
      if (ok) shown += 1;
    });
    const counter = document.getElementById('pnl-match-count');
    if (counter) counter.textContent = String(shown);
  }

  function apply() {
    applyColumns();
    applySort();
    applyFilters();
    saveState();
  }

  function closeDropdowns(except) {
    document.querySelectorAll('#pnl-adv .wl-dd.open').forEach((dd) => {
      if (dd !== except) dd.classList.remove('open');
    });
  }

  function dropdown(label, icon, panel) {
    const btn = el('button', {
      class: 'btn-filter wl-dd-toggle', type: 'button',
      'aria-haspopup': 'true', 'aria-expanded': 'false',
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

  function buildColumnsPanel() {
    const hidden = new Set(state.hidden);
    const items = COLUMNS.filter((c) => !c.locked).map((c) => {
      const cb = el('input', {
        type: 'checkbox', class: 'wl-col-check',
        checked: hidden.has(c.col) ? null : 'checked',
        onchange: (e) => {
          if (e.target.checked) {
            state.hidden = state.hidden.filter((x) => x !== c.col);
          } else if (!state.hidden.includes(c.col)) {
            state.hidden.push(c.col);
            // Drop any sort key on a column the user just hid, so the table
            // can't stay ordered by an invisible key with no indicator.
            state.sortKeys = state.sortKeys.filter((k) => k.field !== c.col);
          }
          apply();
        },
      });
      if (!hidden.has(c.col)) cb.checked = true;
      return el('label', { class: 'wl-col-item' }, [cb, el('span', { text: c.label })]);
    });
    const reset = el('button', {
      class: 'wl-panel-action', type: 'button', text: 'Reset to default columns',
      onclick: () => { state.hidden = DEFAULT_HIDDEN.slice(); render(); },
    });
    return el('div', { class: 'wl-dd-panel wl-columns-panel' }, items.concat([reset]));
  }

  function buildAdv() {
    const adv = document.getElementById('pnl-adv');
    if (!adv) return;
    adv.innerHTML = '';
    adv.appendChild(dropdown('Columns', 'layout-three-columns', buildColumnsPanel()));
  }

  function syncStatic() {
    const search = document.getElementById('pnl-search');
    if (search) search.value = state.query;
  }

  function render() {
    buildAdv();
    syncStatic();
    apply();
  }

  function init() {
    const body = tbody();
    if (!body) return; // not the P&L partial
    originalRows = Array.prototype.slice.call(body.rows).filter(
      (r) => r.querySelector('[data-col]'),
    );
    render();
  }

  document.body.addEventListener('click', (event) => {
    const sortBtn = event.target.closest('#pnl-table .wl-sort');
    if (sortBtn) {
      handleSort(sortBtn.dataset.col, event.shiftKey);
      return;
    }
    if (!event.target.closest('#pnl-adv .wl-dd')) closeDropdowns(null);
  });

  document.body.addEventListener('input', (event) => {
    if (event.target.id === 'pnl-search') {
      state.query = event.target.value;
      apply();
    }
  });

  document.body.addEventListener('htmx:afterSettle', (event) => {
    if (event.detail.target && event.detail.target.id === 'tab-content') init();
  });

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();
