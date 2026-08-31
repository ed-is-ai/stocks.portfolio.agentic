// One global refresh binding controls every action in the Refresh Data menu.
(function () {
  const refreshButton = document.getElementById('refresh-data-button');
  const refreshDropdown = refreshButton?.closest('.refresh-dropdown');
  if (!refreshButton || !refreshDropdown) return;
  const actionButtons = refreshDropdown.querySelectorAll('button[type="submit"]');
  let isRunning = false;

  function setRunning(running) {
    isRunning = running;
    refreshButton.setAttribute('aria-disabled', String(running));
    refreshButton.classList.toggle('loading', running);
    actionButtons.forEach((button) => { button.disabled = running; });
  }

  function formatRelativeAge(dateTime) {
    const refreshedAt = Date.parse(dateTime);
    if (!Number.isFinite(refreshedAt)) return null;
    const ageSeconds = Math.max(0, (Date.now() - refreshedAt) / 1000);
    if (ageSeconds < 60) return 'less than a minute ago';
    const minutes = Math.floor(ageSeconds / 60);
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.floor(ageSeconds / 3600);
    if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.floor(ageSeconds / 86400);
    return `${days} day${days === 1 ? '' : 's'} ago`;
  }

  function refreshRelativeAges() {
    refreshDropdown.querySelectorAll('[data-refresh-age]').forEach((element) => {
      const age = formatRelativeAge(element.dataset.refreshAge);
      if (age) element.textContent = age;
    });
  }

  function syncStatusSlot() {
    const description = document.getElementById('refresh-freshness-description');
    if (!description) return;
    refreshDropdown.style.setProperty(
      '--refresh-status-height',
      `${Math.ceil(description.getBoundingClientRect().height)}px`
    );
  }

  refreshButton.addEventListener('shown.bs.dropdown', () => {
    refreshDropdown.classList.add('status-open');
    refreshRelativeAges();
    window.requestAnimationFrame(syncStatusSlot);
  });
  refreshButton.addEventListener('hidden.bs.dropdown', () => {
    refreshDropdown.classList.remove('status-open');
  });
  refreshButton.addEventListener('pointerenter', refreshRelativeAges);
  refreshButton.addEventListener('focus', refreshRelativeAges);
  refreshButton.addEventListener('click', (event) => {
    if (!isRunning) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  window.addEventListener('resize', () => {
    if (refreshButton.classList.contains('show')) syncStatusSlot();
  });
  document.fonts?.ready.then(() => {
    if (refreshButton.classList.contains('show')) syncStatusSlot();
  });

  document.body.addEventListener('htmx:beforeRequest', (event) => {
    const requestPath = event.detail.requestConfig?.path
      || event.detail.pathInfo?.requestPath
      || event.detail.elt?.getAttribute('hx-post');
    if (requestPath === '/refresh-data') {
      window.bootstrap?.Dropdown.getOrCreateInstance(refreshButton).hide();
      refreshButton.focus();
      setRunning(true);
      // Give the service time to create the durable run artifact, then let the
      // running status partial own its two-second polling lifecycle.
      window.setTimeout(() => {
        htmx.ajax('GET', '/pipeline-status', {
          target: '#pipeline-status',
          swap: 'innerHTML'
        });
      }, 250);
    }
  });

  document.body.addEventListener('htmx:afterRequest', (event) => {
    const requestPath = event.detail.requestConfig?.path
      || event.detail.pathInfo?.requestPath
      || event.detail.elt?.getAttribute('hx-post');
    if (requestPath === '/refresh-data') setRunning(false);
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target.id !== 'pipeline-status') return;
    const state = event.detail.target.querySelector('[data-pipeline-state]')?.dataset.pipelineState;
    setRunning(state === 'running');
  });

  document.body.addEventListener('htmx:oobAfterSwap', (event) => {
    if (event.target.id !== 'refresh-freshness' || !refreshButton.classList.contains('show')) return;
    refreshRelativeAges();
    window.requestAnimationFrame(syncStatusSlot);
  });

  document.body.addEventListener('htmx:responseError', (event) => {
    const requestPath = event.detail.requestConfig?.path
      || event.detail.pathInfo?.requestPath
      || event.detail.elt?.getAttribute('hx-post');
    if (requestPath === '/refresh-data') setRunning(false);
  });
})();
