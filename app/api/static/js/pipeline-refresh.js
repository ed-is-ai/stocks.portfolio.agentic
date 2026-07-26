// One global refresh binding controls the top-level refresh buttons — the
// fast cached "Refresh Data" and the slower "Refresh Institutional Data".
(function () {
  const refreshButtons = [
    document.getElementById('refresh-data-button'),
    document.getElementById('refresh-institutional-button'),
  ].filter(Boolean);
  if (!refreshButtons.length) return;

  function setRunning(running) {
    // A run started from either button blocks both — only one pipeline runs.
    for (const button of refreshButtons) {
      button.disabled = running;
      button.classList.toggle('loading', running);
    }
  }

  document.body.addEventListener('htmx:beforeRequest', (event) => {
    const requestPath = event.detail.requestConfig?.path
      || event.detail.pathInfo?.requestPath
      || event.detail.elt?.getAttribute('hx-post');
    if (requestPath === '/refresh-data') {
      // Give the service time to create the durable run artifact, then let the
      // running status partial own its two-second polling lifecycle.
      window.setTimeout(() => {
        htmx.ajax('GET', '/pipeline-status', {
          target: '#pipeline-status',
          swap: 'innerHTML'
        });
      }, 250);
    }
    if (refreshButtons.includes(event.detail.elt)) setRunning(true);
  });

  document.body.addEventListener('htmx:afterRequest', (event) => {
    if (refreshButtons.includes(event.detail.elt)) setRunning(false);
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    if (event.detail.target.id !== 'pipeline-status') return;
    const state = event.detail.target.querySelector('[data-pipeline-state]')?.dataset.pipelineState;
    setRunning(state === 'running');
  });

  document.body.addEventListener('htmx:responseError', (event) => {
    if (refreshButtons.includes(event.detail.elt)) setRunning(false);
  });
})();
