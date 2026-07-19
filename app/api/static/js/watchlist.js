// Client-side watchlist filters survive HTMX swaps through delegated events.
(function () {
  const activeFilters = new Set();

  function applyFilters() {
    document.querySelectorAll('#tab-content tbody tr[data-sepa]').forEach((row) => {
      const sepaOk = !activeFilters.has('sepa8') || row.dataset.sepa === '8';
      const alertOk = !activeFilters.has('alert') || row.dataset.rec === 'alert';
      const breakoutOk = !activeFilters.has('breakout') || row.dataset.breakout === 'true';
      const mybOk = !activeFilters.has('myb') || row.dataset.myb === 'true';
      const ukOk = !activeFilters.has('uk') || row.dataset.market === 'uk';
      row.style.display = (sepaOk && alertOk && breakoutOk && mybOk && ukOk) ? '' : 'none';
    });
  }

  document.body.addEventListener('click', (event) => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;

    const filter = button.dataset.filter;
    if (activeFilters.has(filter)) {
      activeFilters.delete(filter);
      button.classList.remove('active');
    } else {
      activeFilters.add(filter);
      button.classList.add('active');
    }
    applyFilters();
  });

  document.body.addEventListener('htmx:afterSettle', (event) => {
    if (event.detail.target.id === 'tab-content') activeFilters.clear();
  });
})();
