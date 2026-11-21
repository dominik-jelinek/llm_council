"""Submission failures preserve the unsent draft and link to working Setup controls."""

import json
from unittest.mock import patch

from playwright.sync_api import expect

from backend.config import Settings
from tests.fixtures import CONFIGURATION


def check_failures(browser, base_url, app, model_calls):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(base_url)
        draft = page.locator("#message-input")
        draft.fill("Keep this question until Setup is fixed")
        with patch.object(
            app.state.runs.credentials,
            "get",
            return_value=Settings(_env_file=None, openrouter_api_key=None),
        ):
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#request-error")).to_contain_text("Add an OpenRouter API key")
            expect(draft).to_have_value("Keep this question until Setup is fixed")
            expect(draft).to_be_enabled()
            page.get_by_role("link", name="Open Setup").click()
            dialog = page.get_by_role("dialog", name="Setup")
            expect(dialog).to_be_visible()
            expect(dialog.get_by_label("API key", exact=True)).to_have_value("")
        dialog.get_by_label("API key", exact=True).fill("browser-error-test-key")
        dialog.get_by_role("button", name="Save settings", exact=True).click()
        expect(dialog.get_by_role("status")).to_contain_text("Settings saved")
        expect(dialog.get_by_label("API key", exact=True)).to_have_value("")
        dialog.get_by_role("link", name="Close setup").click()
        expect(draft).to_have_value("Keep this question until Setup is fixed")

        app.state.council_settings.path.write_text(
            json.dumps(CONFIGURATION.model_dump(mode="json") | {"members": []})
        )
        try:
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#request-error")).to_contain_text("at least one council model")
            expect(draft).to_have_value("Keep this question until Setup is fixed")
            expect(page.get_by_role("link", name="Open Setup")).to_be_visible()
        finally:
            app.state.council_settings.save(CONFIGURATION)

        with patch.object(app.state.store, "save", side_effect=OSError("disk full")):
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator("#request-error")).to_contain_text("question could not be saved")
            expect(draft).to_have_value("Keep this question until Setup is fixed")
            expect(draft).to_be_enabled()
        page.reload()
        expect(draft).to_have_value("Keep this question until Setup is fixed")
        assert app.state.store.list() == []
        assert model_calls == []
        assert not errors, errors
        print(
            "PASS: missing key, empty models, storage failure, preserved drafts, Open Setup",
            flush=True,
        )
    finally:
        page.close()
