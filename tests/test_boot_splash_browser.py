"""Exercise the actual splash script/CSS without starting a database or server."""

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("outcome", ["ready", "failed", "unavailable", "stalled"])
def test_splash_holds_for_preparation_and_releases(outcome):
    template = (ROOT / "app/api/templates/index.html").read_text()
    splash = template.split('<div id="boot-splash"', 1)[1].split("</script>", 1)[0]
    css = (ROOT / "app/api/static/css/splash.css").read_text()
    html = (
        f"<html><head><style>{css}</style></head><body>"
        f'<div id="boot-splash"{splash}</script><div id="tab-content"></div>'
        "</body></html>"
    )
    state = {"status": "pending"}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()

            def respond(route):
                if route.request.url.endswith("/startup-status"):
                    if state["status"] == "stalled":
                        return  # Exercise fetch's AbortController timeout.
                    if state["status"] == "unavailable":
                        route.fulfill(status=503, body="unavailable")
                    else:
                        route.fulfill(json=state)
                else:
                    route.fulfill(content_type="text/html", body=html)

            page.route("http://boot.test/**", respond)
            page.goto("http://boot.test/")
            page.wait_for_function(
                "document.querySelector('#boot-splash').dataset.preparation === 'pending'"
            )
            # Both the original JS ceiling (3s) and CSS failsafe (4s) must pass.
            page.wait_for_timeout(4500)
            assert page.locator("#boot-splash").is_visible()
            state["status"] = outcome
            page.locator("#boot-splash").wait_for(state="hidden", timeout=5000)
            assert page.locator("#boot-preparation-warning").is_visible() == (
                outcome != "ready"
            )
        finally:
            browser.close()


@pytest.mark.parametrize("first_gate", ["fonts", "tab"])
def test_preparation_ready_preserves_font_and_tab_gates(first_gate):
    template = (ROOT / "app/api/templates/index.html").read_text()
    splash = template.split('<div id="boot-splash"', 1)[1].split("</script>", 1)[0]
    css = (ROOT / "app/api/static/css/splash.css").read_text()
    html = (
        f"<html><head><style>{css}</style></head><body><script>"
        "window.htmx = {}; Object.defineProperty(document, 'fonts', {value: "
        "{ready: new Promise(resolve => { window.resolveBootFonts = resolve; })}});"
        f'</script><div id="boot-splash"{splash}</script>'
        '<div id="tab-content"></div></body></html>'
    )
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "http://boot.test/**",
                lambda route: (
                    route.fulfill(json={"status": "ready"})
                    if route.request.url.endswith("/startup-status")
                    else route.fulfill(content_type="text/html", body=html)
                ),
            )
            page.goto("http://boot.test/")
            actions = {
                "fonts": "window.resolveBootFonts()",
                "tab": "document.querySelector('#tab-content').dispatchEvent(new Event('htmx:afterSwap', {bubbles: true}))",
            }
            page.evaluate(actions[first_gate])
            page.wait_for_timeout(100)
            assert page.locator("#boot-splash").is_visible()
            page.evaluate(actions["tab" if first_gate == "fonts" else "fonts"])
            page.locator("#boot-splash").wait_for(state="hidden", timeout=1500)
        finally:
            browser.close()


def test_a_fast_failed_startup_still_shows_the_splash_briefly():
    """Preparation failing at once must not flash the splash away."""
    template = (ROOT / "app/api/templates/index.html").read_text()
    splash = template.split('<div id="boot-splash"', 1)[1].split("</script>", 1)[0]
    css = (ROOT / "app/api/static/css/splash.css").read_text()
    html = (
        f"<html><head><style>{css}</style></head><body>"
        '<div id="boot-preparation-warning" hidden></div>'
        f'<div id="boot-splash"{splash}</script><div id="tab-content"></div>'
        "<script>document.querySelector('#tab-content').dispatchEvent("
        "new Event('htmx:afterSwap', {bubbles: true}));</script></body></html>"
    )
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "http://boot.test/**",
                lambda route: (
                    route.fulfill(json={"status": "failed"})
                    if route.request.url.endswith("/startup-status")
                    else route.fulfill(content_type="text/html", body=html)
                ),
            )
            page.goto("http://boot.test/")
            page.wait_for_timeout(400)
            assert page.locator("#boot-splash").is_visible()
            page.locator("#boot-splash").wait_for(state="hidden", timeout=1500)
        finally:
            browser.close()
