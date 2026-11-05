// App shell (GH-128, AD-13): the one owner of the shell's globals.
//
// Globals defined here, and only here:
//   activePortfolio()        selected portfolio: URL ?portfolio_id= wins,
//                            else this browser's last choice (AD-12)
//   setActivePortfolio(id)   remember the browser's choice
//   selectPortfolio(id)      account picker: remember, re-point the URL,
//                            reload the current tab and the badge
//   shellParams()            the values every shell partial request carries
//   setActiveTab(slug)       mark a top-bar tab active ('' clears all)
//   replaceUrl(path)         debounced history.replaceState
//   activeChartRange() / setChartRange(range)
//   showChartMarkers() / setChartMarkers(on) / toggleChartMarkers()
//   syncChartMarkerButton()
//   swapTabContent(html)     swap a full partial into #tab-content via htmx
//
// Back/forward never restores a localStorage snapshot: the history cache
// is off and a miss reloads the page URL, which renders the full shell.
(function () {
  if (window.htmx) {
    htmx.config.historyCacheSize = 0;
    htmx.config.refreshOnHistoryMiss = true;
  }
})();

function activePortfolio() {
  const fromUrl = new URLSearchParams(location.search).get('portfolio_id');
  if (fromUrl) return fromUrl;
  try {
    return localStorage.getItem('activePortfolioId') || '';
  } catch (e) {
    return '';
  }
}

function setActivePortfolio(id) {
  try {
    if (id === null || id === undefined || id === '') {
      localStorage.removeItem('activePortfolioId');
    } else {
      localStorage.setItem('activePortfolioId', String(id));
    }
  } catch (e) {
    /* localStorage unavailable (private mode, disabled) — no-op */
  }
}

function shellParams() {
  return { portfolio_id: activePortfolio(), range: activeChartRange() };
}

function selectPortfolio(id) {
  setActivePortfolio(id);
  const params = new URLSearchParams(location.search);
  if (id) params.set('portfolio_id', id); else params.delete('portfolio_id');
  const query = params.toString();
  try { history.replaceState(history.state, '', location.pathname + (query ? '?' + query : '')); } catch (e) { /* rate-limited */ }
  if (!window.htmx) return;
  // The page URL names its partial (AD-3): /<tab>[/<sub>] -> /partials/...
  htmx.ajax('GET', '/partials' + location.pathname, {
    target: '#tab-content', swap: 'innerHTML', values: shellParams()
  });
  if (document.getElementById('notif-badge')) htmx.trigger('#notif-badge', 'refresh');
}

function setActiveTab(slug) {
  document.querySelectorAll('#shell-tabs [data-tab]').forEach((link) => {
    const on = link.dataset.tab === slug;
    link.classList.toggle('active', on);
    if (on) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
}

// replaceState is debounced (the last URL within 250ms wins) and guarded,
// so fast navigation cannot hit the browser's rate limit.
let replaceUrlTimer = null;
function replaceUrl(path) {
  clearTimeout(replaceUrlTimer);
  replaceUrlTimer = setTimeout(() => {
    try { history.replaceState(history.state, '', path); } catch (err) { /* rate-limited */ }
  }, 250);
}

// Portfolio value-chart range is per-browser too (#421). Absent -> "12M".
function activeChartRange() {
  try {
    return localStorage.getItem('activeChartRange') || '12M';
  } catch (e) {
    return '12M';
  }
}
function setChartRange(range) {
  try {
    if (!range) {
      localStorage.removeItem('activeChartRange');
    } else {
      localStorage.setItem('activeChartRange', String(range));
    }
  } catch (e) {
    /* localStorage unavailable (private mode, disabled) — no-op */
  }
}

// Chart markers are per-browser too, and default to HIDDEN: on a busy
// portfolio the buy/sell triangles and the amber estimate diamonds land on
// nearly every point and bury the value line the chart exists to show
// (#542). Private mode must fall back to the default, never throw.
function showChartMarkers() {
  try {
    return localStorage.getItem('showChartMarkers') === '1';
  } catch (e) {
    return false;
  }
}
function setChartMarkers(on) {
  try {
    if (on) {
      localStorage.setItem('showChartMarkers', '1');
    } else {
      localStorage.removeItem('showChartMarkers');
    }
  } catch (e) {
    /* localStorage unavailable (private mode, disabled) — no-op */
  }
}

// Pure client-side (#542): what "on" means lives with the chart, in
// __portfolioApplyMarkers; here we just move the preference and ask.
function toggleChartMarkers() {
  const on = !showChartMarkers();
  setChartMarkers(on);
  if (window.__portfolioApplyMarkers) window.__portfolioApplyMarkers(on);
  syncChartMarkerButton();
}

// The toggle lives in the always-rendered card shell, so every render of
// that shell has to restate the persisted choice on it.
function syncChartMarkerButton() {
  const btn = document.getElementById('chartMarkerToggle');
  if (!btn) return;
  const on = showChartMarkers();
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.classList.toggle('btn-primary', on);
  btn.classList.toggle('btn-outline-secondary', !on);
  const label = btn.querySelector('[data-marker-label]');
  if (label) label.textContent = on ? 'Hide markers' : 'Show markers';
}

// Swap a full-page partial into #tab-content *through htmx* (#541):
// `el.innerHTML = html` never runs the fragment's scripts or hx-* wiring.
function swapTabContent(html) {
  const target = document.getElementById('tab-content');
  if (!target) return;
  if (window.htmx && typeof window.htmx.swap === 'function') {
    htmx.swap('#tab-content', html, { swapStyle: 'innerHTML' });
  } else {
    target.innerHTML = html;
  }
}

(function () {
  // A URL's ?portfolio_id= becomes this browser's choice, so every tab and
  // a later visit without it show the same portfolio.
  const fromUrl = new URLSearchParams(location.search).get('portfolio_id');
  if (fromUrl) setActivePortfolio(fromUrl);

  // A tab click marks itself active at once; its partial and pushed URL
  // come from its own hx-get / hx-push-url.
  document.getElementById('shell-tabs')?.addEventListener('click', (e) => {
    const link = e.target.closest('[data-tab]');
    if (link) setActiveTab(link.dataset.tab);
  });
  // A ⚙ menu page is no tab.
  document.getElementById('shell-gear-menu')?.addEventListener('click', (e) => {
    if (e.target.closest('[data-page]')) setActiveTab('');
  });

  // Swaps from in-content forms: a trade lands on the Portfolio, a refresh
  // on the Scanner. Match the route rather than treating every form alike.
  document.body.addEventListener('htmx:afterSettle', (e) => {
    if (e.detail.target.id !== 'tab-content') return;
    const post = e.detail.elt?.getAttribute('hx-post');
    if (post === '/trades') {
      setActiveTab('portfolio');
      replaceUrl('/portfolio');
    } else if (post === '/refresh-data') {
      setActiveTab('scanner');
    }
  });

  // Programmatic focus on a tab swap lands on the H1 (rings via
  // :focus-visible only).
  document.body.addEventListener('htmx:afterSettle', (e) => {
    if (e.detail.target.id !== 'tab-content' || !e.detail.elt?.closest?.('#shell-tabs')) return;
    document.querySelector('#tab-content h1')?.focus({ preventScroll: true });
  });

  // A changed badge count is announced politely (not the first one).
  let lastBadgeAnnouncement = null;
  document.body.addEventListener('htmx:afterSettle', () => {
    const text = document.getElementById('notif-badge')?.dataset.announce;
    if (text === undefined || text === lastBadgeAnnouncement) return;
    if (lastBadgeAnnouncement !== null) document.getElementById('notif-live').textContent = text;
    lastBadgeAnnouncement = text;
  });
})();
