// Strategy Manager (Story 2.7/2.8): monotonic Backtest-activity polling and
// navigation-vs-background-poll focus management. Mirrors
// pipeline-refresh.js/watchlist.js's existing shape -- vanilla JS, listeners
// on document.body scoped by event.detail.target.id, no build step, no
// external dependencies.
(function () {
  'use strict';

  // A poll fragment/target is anything carrying data-status-version, or an
  // activity section id (the only elements this story stamps with it) --
  // scoped narrowly so unrelated htmx swaps elsewhere in the app are never
  // touched by this gate.
  function isVersionedActivityTarget(el) {
    return !!el && !!el.getAttribute && (
      (el.id && el.id.indexOf('backtest-activity-') === 0) ||
      el.hasAttribute('data-status-version')
    );
  }

  function currentVersion(el) {
    var raw = el.getAttribute('data-status-version');
    if (raw === null || raw === '') return null;
    var value = Number(raw);
    return Number.isNaN(value) ? null : value;
  }

  function incomingVersion(html) {
    var match = /data-status-version="(-?[0-9]+)"/.exec(html || '');
    if (!match) return null;
    var value = Number(match[1]);
    return Number.isNaN(value) ? null : value;
  }

  // The activity section's own "every 3s" poll is the only GET request that
  // ever swaps its id -- Cancel/Restart/Delete are POSTs. Matching the
  // request path is a more reliable signal than the request verb (not every
  // htmx event on this element carries requestConfig) and mirrors
  // pipeline-refresh.js's existing pathInfo-reading convention. Anchored to
  // the path's end (not a bare substring) so a future unrelated route
  // segment containing "/status" can never be misclassified as this poll.
  function isBackgroundPoll(detail) {
    var path = (detail.requestConfig && detail.requestConfig.path) ||
      (detail.pathInfo && detail.pathInfo.requestPath) || '';
    return (/\/status(?:\?|$)/).test(path);
  }

  // ── Monotonic version gate (Story 2.8 AC4): an out-of-order older
  // response must never clobber an already-rendered newer one. ──────────
  document.body.addEventListener('htmx:beforeSwap', function (event) {
    var detail = event.detail;
    var target = detail.target;
    if (!isVersionedActivityTarget(target)) return;

    var incoming = incomingVersion(detail.serverResponse);
    var current = currentVersion(target);
    if (incoming === null || current === null) return; // nothing to compare
    if (incoming <= current) {
      detail.shouldSwap = false;
    }
  });

  // ── Preserve scroll position and the focused control across a
  // background poll's outerHTML swap (Story 2.8 AC4, AC8). ──────────────
  var pending = null; // {scrollX, scrollY, focusedId} captured just before a poll swap

  document.body.addEventListener('htmx:beforeSwap', function (event) {
    var detail = event.detail;
    if (!isVersionedActivityTarget(detail.target) || !isBackgroundPoll(detail)) return;
    var active = document.activeElement;
    pending = {
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      focusedId: active && detail.target.contains(active) && active.id ? active.id : null,
    };
  });

  document.body.addEventListener('htmx:afterSwap', function (event) {
    if (!pending) return;
    var captured = pending;
    pending = null;
    if (!isVersionedActivityTarget(event.detail.target)) return;
    window.scrollTo(captured.scrollX, captured.scrollY);
    if (captured.focusedId) {
      var restored = document.getElementById(captured.focusedId);
      if (restored) restored.focus({ preventScroll: true });
    }
  });

  // ── Heading/error-summary focus on real user navigation into
  // #tab-content, and on a Story 2.9 note-save swap (#backtest-note-*) --
  // never on the in-place activity poll, which swaps the narrower
  // #backtest-activity-{id} section instead (Story 2.7 AC7,9; Story 2.8
  // AC4; Story 2.9 AC7,8). Story 2.8 scopes the version gate/poll-safe
  // focus handling to Backtest activity only -- Initialization's
  // pre-existing polling is untouched by this story. ────────────────────
  document.body.addEventListener('htmx:afterSettle', function (event) {
    var target = event.detail && event.detail.target;
    var isNoteSwap = target && target.id && target.id.indexOf('backtest-note-') === 0;
    if (!target || (target.id !== 'tab-content' && !isNoteSwap)) return;
    // A 422 response's linked error summary takes priority over the
    // heading once per invalid submit (the same alert/tabindex="-1"
    // pattern _strategy_configuration.html and _historical_initialization
    // .html both already use).
    var errorSummary = target.querySelector('[role="alert"][tabindex="-1"]');
    if (errorSummary) {
      errorSummary.focus();
      return;
    }
    var heading = target.querySelector('[tabindex="-1"]');
    if (heading) heading.focus();
  });
})();
