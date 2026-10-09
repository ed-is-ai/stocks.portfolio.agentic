from __future__ import annotations

import pytest

from app.api.templating import templates
from tests.test_strategy_experiment_routes import _detail

playwright = pytest.importorskip("playwright.sync_api")


def test_approval_dialog_traps_focus_and_restores_trigger() -> None:
    markup = templates.get_template("_strategy_experiment_detail.html").render(
        detail=_detail(), error=None
    )
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binaries are optional in local checkouts
            pytest.skip(f"Chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(markup)
            trigger = page.locator("#experiment-review-trigger")
            dialog = page.locator("#experiment-approval-dialog")
            cancel = page.locator("[data-close-experiment-dialog]")
            approval_checkbox = page.locator("#confirm-experiment-approval")
            trigger.click()

            assert dialog.evaluate("element => element.open")
            assert cancel.evaluate("element => element === document.activeElement")

            cancel.focus()
            page.keyboard.press("Tab")
            assert approval_checkbox.evaluate(
                "element => element === document.activeElement"
            )

            page.keyboard.press("Shift+Tab")
            assert cancel.evaluate("element => element === document.activeElement")

            page.keyboard.press("Escape")
            assert not dialog.evaluate("element => element.open")
            assert trigger.evaluate("element => element === document.activeElement")
        finally:
            browser.close()
