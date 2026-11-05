// One global refresh binding controls every refresh action: the shell's
// single Refresh and the variants in Settings (GH-128).
(function () {
  const refreshButton = document.getElementById('refresh-data-button');
  if (!refreshButton) return;
  let isRunning = false;

  function actionButtons() {
    return document.querySelectorAll('[data-refresh-action] button[type="submit"]');
  }

  function setRunning(running) {
    isRunning = running;
    refreshButton.setAttribute('aria-disabled', String(running));
    refreshButton.classList.toggle('loading', running);
    actionButtons().forEach((button) => { button.disabled = running; });
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
    document.querySelectorAll('[data-refresh-age]').forEach((element) => {
      const age = formatRelativeAge(element.dataset.refreshAge);
      if (age) element.textContent = age;
    });
  }

  refreshButton.addEventListener('pointerenter', refreshRelativeAges);
  refreshButton.addEventListener('focus', refreshRelativeAges);
  // A refresh already running ignores further clicks on any refresh action.
  document.addEventListener('click', (event) => {
    if (!isRunning || !event.target.closest?.('[data-refresh-action] button[type="submit"]')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  function requestPath(event) {
    return event.detail.requestConfig?.path
      || event.detail.pathInfo?.requestPath
      || event.detail.elt?.getAttribute('hx-post');
  }

  document.body.addEventListener('htmx:beforeRequest', (event) => {
    if (requestPath(event) !== '/refresh-data') return;
    setRunning(true);
    // Give the service time to create the durable run artifact, then let the
    // running status partial own its two-second polling lifecycle.
    window.setTimeout(() => {
      htmx.ajax('GET', '/pipeline-status', {
        target: '#pipeline-status',
        swap: 'innerHTML'
      });
    }, 250);
  });

  document.body.addEventListener('htmx:afterRequest', (event) => {
    if (requestPath(event) === '/refresh-data') setRunning(false);
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target.id === 'tab-content') refreshRelativeAges();
    if (event.detail.target.id !== 'pipeline-status') return;
    const state = event.detail.target.querySelector('[data-pipeline-state]')?.dataset.pipelineState;
    setRunning(state === 'running');
  });

  document.body.addEventListener('htmx:responseError', (event) => {
    if (requestPath(event) === '/refresh-data') setRunning(false);
  });
})();
