"""Real-browser checks against an isolated server with fake model responses.

Run: uv run --with playwright python -m tests.browser_smoke
Install Chromium first with: uv run --with playwright playwright install chromium
An existing browser can be selected with PLAYWRIGHT_CHROMIUM_EXECUTABLE.
"""

import asyncio
import os
import socket
import tempfile
import threading
import time
from unittest.mock import patch

import uvicorn
from playwright.sync_api import expect, sync_playwright

from backend import council
from backend.failures import ProviderError
from backend.main import create_app
from tests.browser_failures import check_failures
from tests.browser_projects import check_projects
from tests.browser_sidebar import check_sidebar_scroll
from tests.fixtures import (
    MODELS,
    RANKINGS,
    RESPONSES,
    configure_test_app,
    parallel_reply,
    single_reply,
)


def check_composer(page):
    setup = page.get_by_role("button", name="Setup").bounding_box()
    new_button = page.get_by_role("button", name="+ New Conversation").bounding_box()
    assert setup["y"] + setup["height"] <= new_button["y"]
    assert page.locator("#sidebar").get_by_role("button", name="Setup").count() == 1
    input_box = page.locator("#message-input")
    input_box.fill("")
    initial = input_box.bounding_box()
    styles = input_box.evaluate("""element => {
        const style = getComputedStyle(element);
        return { lineHeight: parseFloat(style.lineHeight), radius: parseFloat(style.borderRadius) };
    }""")
    assert styles["radius"] >= initial["height"] / 2
    for selector in (".council-switch", ".composer-options .send-button"):
        control = page.locator(selector)
        assert control.bounding_box()["y"] >= initial["y"] + initial["height"]
        expect(control).to_be_in_viewport()

    for lines in (2, 7, 10):
        input_box.fill("\n".join(f"Line {number}" for number in range(lines)))
        size = input_box.bounding_box()
        expected_height = initial["height"] + (min(lines, 7) - 1) * styles["lineHeight"]
        assert abs(size["height"] - expected_height) < 1
        assert abs(size["y"] + size["height"] - initial["y"] - initial["height"]) < 1
        assert input_box.evaluate("element => element.scrollHeight > element.clientHeight") == (
            lines > 7
        )

    input_box.press("Control+End")
    assert input_box.evaluate("element => element.scrollTop > 0")
    input_box.fill("Wrapped text " * 50)
    assert input_box.bounding_box()["height"] > initial["height"]
    assert input_box.bounding_box()["height"] <= initial["height"] + 6 * styles["lineHeight"]
    input_box.fill("")
    assert input_box.bounding_box()["height"] == initial["height"]
    expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()


def main():
    failures = 0
    model_calls = []

    async def delayed_parallel(models, messages):
        nonlocal failures
        prompt = messages[-1]["content"]
        model_calls.append(prompt)
        if prompt == "Delete while running":
            await asyncio.Event().wait()
        if prompt == "Fail once" and failures == 0:
            failures += 1
            await asyncio.sleep(0.15)
            raise ProviderError("authentication")
        await asyncio.sleep(1.0 if "FINAL RANKING:" in prompt else 0.3)
        return await parallel_reply(models, messages)

    async def delayed_single(model, messages, timeout=120.0):
        await asyncio.sleep(0.05 if timeout == 30 else 0.5)
        return await single_reply(model, messages, timeout)

    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(council, "query_models_parallel", delayed_parallel),
        patch.object(council, "query_model", delayed_single),
    ):
        app = create_app(directory)
        configure_test_app(app)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("Test server did not start")
                time.sleep(0.02)
            with sync_playwright() as playwright:
                executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
                browser = playwright.chromium.launch(
                    **({"executable_path": executable} if executable else {})
                )
                check_failures(browser, base_url, app, model_calls)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1050},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                requests = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on("request", lambda request: requests.append(request.url))
                page.goto(base_url)
                page.evaluate("window.smokeSentinel = 'same document'")
                expect(page.locator("#empty-state")).to_be_visible()
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".conversation-item")).to_have_count(0)
                assert page.request.get(base_url + "/api/conversations").json() == []

                def new_conversation(draft=""):
                    with page.expect_response(
                        lambda response: (
                            response.url == base_url + "/conversations"
                            and response.request.method == "POST"
                        )
                    ):
                        page.get_by_role("button", name="+ New Conversation").click()
                    expect(page).to_have_url(base_url + "/")
                    expect(page.locator("#empty-state")).to_be_visible()
                    expect(page.locator("#message-input")).to_have_value(draft)
                    return page.url

                page.locator("#message-input").fill("Unsent draft")
                new_conversation("Unsent draft")
                page.reload()
                expect(page.locator("#message-input")).to_have_value("Unsent draft")
                page.locator("#message-input").fill("")
                expect(page.locator(".conversation-item")).to_have_count(0)
                draft_tab = page.context.new_page()
                draft_tab.goto(base_url)
                expect(draft_tab.locator("#message-input")).to_have_value("")
                draft_tab.locator("#message-input").fill("Unsent in another tab")
                draft_tab.route(
                    "**/conversations/messages",
                    lambda route: route.fulfill(
                        status=503,
                        headers={"HX-Retarget": "#request-error", "HX-Reswap": "innerHTML"},
                        content_type="text/html",
                        body="Temporarily unavailable",
                    ),
                )
                draft_tab.get_by_role("button", name="Send", exact=True).click()
                expect(draft_tab.locator("#request-error")).to_contain_text(
                    "Temporarily unavailable"
                )
                expect(draft_tab.locator("#message-input")).to_have_value("Unsent in another tab")
                draft_tab.reload()
                expect(draft_tab.locator("#message-input")).to_have_value("Unsent in another tab")
                expect(page.locator("#message-input")).to_have_value("")
                draft_tab.close()
                assert page.request.get(base_url + "/api/conversations").json() == []
                page.evaluate("window.smokeSentinel = 'same document'")
                switch = page.get_by_role("switch", name="Council")
                expect(switch).to_be_checked()
                expect(page.locator("#message-input")).to_be_enabled()
                assert page.evaluate("window.smokeSentinel") == "same document"
                expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
                check_composer(page)
                print(
                    "PASS: pill composer, upward growth, seven-line cap, wrapping and shrink",
                    flush=True,
                )

                question = "Explain **Čau** & <tag>"
                page.locator("#message-input").fill(question)
                page.locator("#message-input").press("Shift+Enter")
                page.locator("#message-input").press("t")
                expect(page.locator("#message-input")).to_have_value(question + "\nt")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".user-message")).to_have_count(1)
                expect(page.locator("#message-input")).to_be_disabled()
                first_url = page.url
                assert "/conversations/" in first_url
                expect(switch).to_be_disabled()
                expect(page.locator(".stage1")).to_be_visible()
                expect(page.locator(".stage1")).not_to_have_attribute("open", "")
                expect(page.locator('.stage1 [role="tab"]').first).not_to_be_visible()
                page.locator(".stage1 > summary").click()
                beta = page.locator('.stage1 [role="tab"]').nth(1)
                beta.click()
                expect(beta).to_have_attribute("aria-selected", "true")
                expect(page.locator(".stage3")).to_be_visible()
                expect(page.locator(".stage3")).to_have_attribute("open", "")
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator("#message-input")).to_have_value("")
                expect(beta).to_have_attribute("aria-selected", "true")
                expect(page.locator(".stage1")).to_have_attribute("open", "")
                expect(page.locator(".stage2")).not_to_have_attribute("open", "")
                page.locator(".stage2 > summary").click()
                expect(page.locator(".aggregate-rankings")).to_be_visible()
                expect(page.locator(".stage2 .markdown-content").first).to_contain_text("alpha")
                expect(page.locator(".stage-loading")).to_have_count(0)
                beta.focus()
                beta.press("ArrowLeft")
                expect(page.locator('.stage1 [role="tab"]').first).to_be_focused()
                assert not page.evaluate(
                    "document.querySelector('#messages').scrollWidth > document.querySelector('#messages').clientWidth"
                )
                page.locator("#messages").evaluate("element => element.scrollTop = 0")
                page.screenshot(path="/tmp/llm-council-htmx-desktop.png")
                print(
                    "PASS: first turn, stage updates, tab preservation, keyboard controls, rankings",
                    flush=True,
                )

                interaction = page.locator('.assistant-message > [id^="usage-"] .usage-footer')
                expect(interaction.locator(".usage-cost")).to_have_text("Cost: $0.0091 USD")
                expect(interaction.locator(".usage-cache")).to_have_text("Cached input: 75.0%")
                interaction.get_by_role("button", name="Interaction cost details").click()
                usage_dialog = page.get_by_role("dialog", name="Interaction — cost details")
                expect(usage_dialog).to_be_visible()
                expect(usage_dialog.locator("tbody tr")).to_have_count(6)
                expect(usage_dialog.locator("tbody")).to_contain_text("Title")
                expect(usage_dialog.locator("tbody")).to_contain_text("750")
                expect(usage_dialog.locator("tbody")).to_contain_text("200")
                expect(usage_dialog.locator("tbody")).to_contain_text("50")
                usage_dialog.get_by_role("button", name="Close cost details").click()
                expect(usage_dialog).not_to_be_visible()
                print(
                    "PASS: interaction cost, cached fraction, per-request token details", flush=True
                )

                def assert_copy(name, expected):
                    button = page.get_by_role("button", name=name, exact=True)
                    button.click()
                    expect(button.locator("..").locator(".copy-status")).to_have_text("Copied!")
                    assert page.evaluate("navigator.clipboard.readText()") == expected

                assert_copy("Copy your message", question + "\nt")
                assert_copy("Copy alpha response", RESPONSES[MODELS[0]]["content"])
                assert_copy("Copy alpha evaluation", RANKINGS[MODELS[0]]["content"])
                assert_copy(
                    "Copy final answer", "## Final answer\n\nThe council agrees on a useful answer."
                )
                page.evaluate("""() => {
                    window.savedWriteText = navigator.clipboard.writeText;
                    navigator.clipboard.writeText = async () => { throw new Error('Denied'); };
                }""")
                page.get_by_role("button", name="Copy final answer").click()
                expect(page.locator(".stage3 .copy-status")).to_contain_text("Copy failed")
                page.evaluate("""() => {
                    navigator.clipboard.writeText = window.savedWriteText;
                    window.savedClipboard = navigator.clipboard;
                    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
                }""")
                page.get_by_role("button", name="Copy final answer").click()
                expect(page.locator(".stage3 .copy-status")).to_have_text("Copied!")
                assert page.evaluate("window.savedClipboard.readText()") == (
                    "## Final answer\n\nThe council agrees on a useful answer."
                )
                page.evaluate("delete navigator.clipboard")
                print("PASS: exact Markdown copying, denied clipboard, HTTP fallback", flush=True)

                followup_draft = "An unfinished follow-up\nwith another line"
                page.locator("#message-input").fill(followup_draft)
                new_conversation()
                new_draft = "An unfinished new question"
                page.locator("#message-input").fill(new_draft)
                first_id = first_url.rsplit("/", 1)[1]
                page.locator(f'a.conversation-item[href="/conversations/{first_id}"]').click()
                expect(page.locator(".user-message")).to_have_count(1)
                expect(page.locator("#message-input")).to_have_value(followup_draft)
                page.reload()
                expect(page.locator("#message-input")).to_have_value(followup_draft)
                new_conversation(new_draft)
                page.go_back()
                expect(page.locator("#message-input")).to_have_value(followup_draft)
                page.go_forward()
                expect(page.locator("#message-input")).to_have_value(new_draft)
                page.locator("#message-input").fill("")
                page.locator(f'a.conversation-item[href="/conversations/{first_id}"]').click()
                expect(page.locator("#message-input")).to_have_value(followup_draft)
                expect(page.locator(".conversation-item")).to_have_count(1)
                assert len(page.request.get(base_url + "/api/conversations").json()) == 1
                print(
                    "PASS: separate drafts, reload/history restoration, rejected sends, tab isolation",
                    flush=True,
                )

                before_followup = len(model_calls)
                expect(switch).to_be_checked()
                page.locator("#message-input").fill("Council follow up")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage3")).to_have_count(2)
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator("#message-input")).to_have_value("")
                expect(page.locator(".stage1")).to_have_count(2)
                assert len(model_calls) == before_followup + 2
                print("PASS: full council follow-up enabled by default", flush=True)

                before_followup = len(model_calls)
                switch.focus()
                switch.press("Space")
                expect(switch).not_to_be_checked()
                page.locator("#message-input").fill("Follow up")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage3")).to_have_count(3)
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".stage1")).to_have_count(2)
                assert len(model_calls) == before_followup
                expect(switch).not_to_be_checked()
                page.reload()
                expect(page.locator("#message-input")).to_have_value("")
                expect(page.locator(".stage3")).to_have_count(3)
                expect(page.locator(".stage1[open], .stage2[open]")).to_have_count(0)
                expect(page.locator(".stage3[open]")).to_have_count(3)
                expect(page.locator(".aggregate-rankings")).to_have_count(2)
                expect(switch).not_to_be_checked()
                page.get_by_role("button", name="Copy final answer").last.click()
                expect(page.locator(".stage3 .copy-status").last).to_have_text("Copied!")
                assert (
                    page.evaluate("navigator.clipboard.readText()")
                    == "A contextual follow-up answer."
                )
                print("PASS: chairman-only follow-up and reload persistence", flush=True)

                before_edit = len(model_calls)
                page.locator(".user-message").nth(1).get_by_role("button", name="Edit").click()
                editor = page.get_by_role("textbox", name="Edit your message")
                expect(editor).to_have_value("Council follow up")
                expect(editor).to_be_focused()
                editor.fill("Discard this edit")
                page.get_by_role("link", name="Cancel", exact=True).click()
                expect(editor).to_have_count(0)
                expect(page.locator(".user-message").nth(1)).to_contain_text("Council follow up")
                assert len(model_calls) == before_edit

                page.locator(".user-message").first.get_by_role("button", name="Edit").click()
                expect(editor).to_have_value(question + "\nt")
                editor.fill("Edited **Čau**\nA different question")
                page.get_by_role("button", name="Save & send", exact=True).click()
                expect(page.locator(".branch-position")).to_have_text("2 / 2")
                expect(page.locator(".user-message")).to_have_count(1)
                page.get_by_role("button", name="Previous branch").click()
                expect(page.locator(".branch-position")).to_have_text("1 / 2")
                expect(page.locator(".user-message")).to_have_count(3)
                expect(page.get_by_role("status").filter(has_text="another branch")).to_be_visible()
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".stage3:visible")).to_have_count(3)
                assert len(model_calls) == before_edit + 2
                page.get_by_role("button", name="Next branch").click()
                expect(page.locator(".user-message")).to_have_count(1)
                expect(page.locator(".stage3")).to_have_count(1)
                expect(page.locator(".user-message").first).to_contain_text("A different question")
                expect(page.get_by_role("button", name="Next branch")).to_be_disabled()
                page.reload()
                expect(page.locator(".branch-position")).to_have_text("2 / 2")

                page.locator("#message-input").fill("Follow-up on the edited branch")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage3")).to_have_count(2)
                expect(page.locator("#message-input")).to_be_enabled()
                page.locator(".user-message").nth(1).get_by_role("button", name="Edit").click()
                editor.fill("Edited nested follow-up")
                page.get_by_role("button", name="Save & send", exact=True).click()
                expect(page.locator(".stage3")).to_have_count(2)
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".branch-position")).to_have_text(["2 / 2", "2 / 2"])
                page.locator(".user-message").first.get_by_role("button", name="Edit").click()
                editor.fill("Third version")
                page.get_by_role("button", name="Save & send", exact=True).click()
                expect(page.locator(".stage3")).to_have_count(1)
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".branch-position")).to_have_text("3 / 3")

                before_switching = len(model_calls)
                page.get_by_role("button", name="Previous branch").click()
                expect(page.locator(".branch-position")).to_have_text(["2 / 3", "2 / 2"])
                expect(page.locator(".user-message").nth(1)).to_contain_text(
                    "Edited nested follow-up"
                )
                page.get_by_role("button", name="Previous branch").last.click()
                expect(page.locator(".user-message").nth(1)).to_contain_text(
                    "Follow-up on the edited branch"
                )
                page.get_by_role("button", name="Next branch").last.click()
                expect(page.locator(".branch-position")).to_have_text(["2 / 3", "2 / 2"])
                page.reload()
                expect(page.locator(".branch-position")).to_have_text(["2 / 3", "2 / 2"])
                page.locator("#messages").evaluate("element => element.scrollTop = 0")
                page.screenshot(path="/tmp/llm-council-branches-desktop.png")
                page.get_by_role("button", name="Previous branch").first.click()
                expect(page.locator(".branch-position")).to_have_text(["1 / 3"])
                expect(page.locator(".user-message")).to_have_count(3)
                expect(page.locator(".stage3")).to_have_count(3)
                assert len(model_calls) == before_switching
                page.goto(first_url)
                expect(page.locator(".branch-position")).to_have_text(["3 / 3"])
                expect(page.locator(".user-message")).to_have_count(1)
                print(
                    "PASS: editing, nested branches, active-run navigation, latest branch default",
                    flush=True,
                )

                new_conversation()
                expect(switch).to_be_checked()
                page.locator("#message-input").fill("Keep running")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage1")).to_be_visible()
                running_url = page.url
                page.reload()
                expect(page.locator(".user-message")).to_have_count(1)
                empty_url = new_conversation()
                running_id = running_url.rsplit("/", 1)[1]
                deadline = time.monotonic() + 10
                while (
                    len(
                        page.request.get(base_url + "/api/conversations/" + running_id).json()[
                            "messages"
                        ]
                    )
                    < 2
                ):
                    if time.monotonic() > deadline:
                        raise AssertionError("Background run did not finish")
                    time.sleep(0.05)
                expect(page.locator(".assistant-message")).to_have_count(0)
                page.locator(f'a.conversation-item[href="/conversations/{running_id}"]').click()
                expect(page.locator(".stage3")).to_have_count(1)
                expect(page.locator(".user-message")).to_have_count(1)
                page.go_back()
                expect(page).to_have_url(empty_url)
                expect(page.locator("#empty-state")).to_be_visible()
                expect(page.locator("#message-input")).to_be_enabled()
                assert page.url == empty_url
                expect(page.locator(".assistant-message")).to_have_count(0)
                print(
                    "PASS: reconnect on reload, navigation during a run, browser history",
                    flush=True,
                )

                page.locator("#message-input").fill("Fail once")
                page.locator("#message-input").press("Enter")
                expect(page.get_by_role("button", name="Retry", exact=True)).to_be_visible()
                expect(page.locator("#request-error")).to_contain_text("rejected the API key")
                expect(page.locator(".stage-loading")).to_have_count(0)
                page.reload()
                expect(page.locator("#request-error")).to_contain_text("rejected the API key")
                page.get_by_role("link", name="Open Setup").click()
                recovery = page.get_by_role("dialog", name="Setup")
                expect(recovery).to_be_visible()
                recovery.get_by_label("API key", exact=True).fill("browser-replacement-key")
                recovery.get_by_role("button", name="Save settings", exact=True).click()
                expect(recovery.get_by_role("status")).to_contain_text("Settings saved")
                recovery.get_by_role("link", name="Close setup").click()
                page.get_by_role("button", name="Retry", exact=True).click()
                expect(page.locator(".stage3")).to_have_count(1)
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".user-message")).to_have_count(1)
                expect(page.locator(".assistant-message")).to_have_count(1)
                print("PASS: visible failure and retry without duplicate messages", flush=True)

                page.get_by_role("button", name="Setup").click()
                dialog = page.get_by_role("dialog", name="Setup")
                expect(dialog).to_be_visible()
                api_key = dialog.get_by_label("API key", exact=True)
                expect(api_key).to_have_attribute("type", "password")
                api_key.fill("browser-test-openrouter-key")
                models = dialog.locator('input[name="member_model"]')
                efforts = dialog.locator('select[name="member_effort"]')
                expect(models).to_have_count(2)
                dialog.get_by_role("button", name="+ Add model").click()
                expect(models).to_have_count(3)
                dialog.get_by_role("button", name="Remove council member").last.click()
                expect(models).to_have_count(2)
                models.nth(0).fill(MODELS[0])
                models.nth(1).fill(MODELS[0])
                efforts.nth(0).select_option("high")
                efforts.nth(1).select_option("high")
                dialog.locator('select[name="chairman_effort"]').select_option("medium")
                dialog.get_by_role("button", name="Save settings").click()
                expect(dialog.get_by_role("status")).to_contain_text("Settings saved")
                expect(api_key).to_have_value("")
                expect(api_key).to_have_attribute(
                    "placeholder", "Key configured — leave blank to keep it"
                )
                assert (
                    app.state.runs.credentials.get().openrouter_api_key.get_secret_value()
                    == "browser-test-openrouter-key"
                )
                assert "browser-test-openrouter-key" not in page.content()
                assert not page.evaluate(
                    "JSON.stringify([localStorage, sessionStorage]).includes('browser-test-openrouter-key')"
                )
                dialog.get_by_role("link", name="Close setup").click()
                expect(dialog).not_to_be_visible()
                page.reload()
                page.get_by_role("button", name="Setup").click()
                expect(dialog).to_be_visible()
                expect(models.nth(1)).to_have_value(MODELS[0])
                expect(api_key).to_have_value("")
                expect(api_key).to_have_attribute(
                    "placeholder", "Key configured — leave blank to keep it"
                )
                expect(efforts.nth(0)).to_have_value("high")
                expect(efforts.nth(1)).to_have_value("high")
                expect(dialog.locator('select[name="chairman_effort"]')).to_have_value("medium")
                page.screenshot(path="/tmp/llm-council-setup-desktop.png")
                page.keyboard.press("Escape")
                expect(dialog).not_to_be_visible()
                new_conversation()
                page.locator("#message-input").fill("Use the configured council")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage3")).to_have_count(1)
                expect(page.locator("#message-input")).to_be_enabled()
                page.locator(".stage1 > summary").click()
                page.locator(".stage2 > summary").click()
                expect(
                    page.locator(".stage1").get_by_role("tab", name="alpha (high) #1", exact=True)
                ).to_be_visible()
                expect(
                    page.locator(".stage1").get_by_role("tab", name="alpha (high) #2", exact=True)
                ).to_be_visible()
                expect(page.locator(".chairman-label")).to_contain_text("chairman (medium)")
                expect(page.locator(".aggregate-item")).to_have_count(2)
                print(
                    "PASS: Setup edits, durable preferences, independent duplicate members",
                    flush=True,
                )

                page.goto(first_url)
                page.locator("#message-input").fill(
                    "A draft that wraps as the viewport narrows. " * 2
                )
                desktop_height = page.locator("#message-input").bounding_box()["height"]
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_function(
                    "height => document.querySelector('#message-input').getBoundingClientRect().height > height",
                    arg=desktop_height,
                )
                check_composer(page)
                expect(page.locator("#message-input")).to_be_in_viewport()
                assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                page.locator("#messages").evaluate("element => element.scrollTop = 0")
                page.screenshot(path="/tmp/llm-council-htmx-mobile.png")
                page.get_by_role("button", name="Interaction cost details").last.click()
                expect(usage_dialog).to_be_visible()
                assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                page.screenshot(path="/tmp/llm-council-usage-mobile.png")
                usage_dialog.get_by_role("button", name="Close cost details").click()
                page.get_by_role("button", name="Setup").click()
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role("button", name="Save settings")).to_be_enabled()
                assert not dialog.evaluate("element => element.scrollWidth > element.clientWidth")
                page.screenshot(path="/tmp/llm-council-setup-mobile.png")
                dialog.get_by_role("link", name="Close setup").click()
                page.locator("#message-input").fill("Keep this unfinished question")
                page.locator(f'a.conversation-item[href="/conversations/{running_id}"]').click()
                expect(page.locator("#message-input")).to_be_enabled()
                page.locator("#message-input").fill("Discard this conversation's draft")
                page.locator(f'a.conversation-item[href="/conversations/{first_id}"]').click()
                expect(page.locator("#message-input")).to_have_value(
                    "Keep this unfinished question"
                )
                page.locator(f'form[action="/conversations/{running_id}/delete"] button').click()
                expect(
                    page.locator(f'a.conversation-item[href="/conversations/{running_id}"]')
                ).to_have_count(0)
                expect(page).to_have_url(first_url)
                expect(page.locator("#message-input")).to_have_value(
                    "Keep this unfinished question"
                )
                assert (
                    page.evaluate(
                        "id => sessionStorage.getItem(`llm-council:draft:${id}`)", running_id
                    )
                    is None
                )
                assert page.request.get(base_url + "/api/conversations/" + running_id).status == 404

                page.locator(f'form[action="/conversations/{first_id}/delete"] button').click()
                expect(page).to_have_url(base_url + "/")
                expect(page.locator("#empty-state")).to_be_visible()
                expect(page.locator("#message-input")).to_have_value("")
                assert (
                    page.evaluate(
                        "id => sessionStorage.getItem(`llm-council:draft:${id}`)", first_id
                    )
                    is None
                )
                page.reload()
                expect(
                    page.locator(f'a.conversation-item[href="/conversations/{first_id}"]')
                ).to_have_count(0)

                page.locator("#message-input").fill("Delete while running")
                page.locator("#message-input").press("Enter")
                expect(page.locator(".stage-loading")).to_have_count(1)
                expect(page.locator("#message-input")).to_be_disabled()
                active_id = page.url.rsplit("/", 1)[1]
                page.locator(f'form[action="/conversations/{active_id}/delete"] button').click()
                expect(page).to_have_url(base_url + "/")
                expect(page.locator("#message-input")).to_be_enabled()
                expect(page.locator(".stage-loading")).to_have_count(0)
                assert active_id not in app.state.runs.active
                assert page.request.get(base_url + "/api/conversations/" + active_id).status == 404

                page.locator("#message-input").fill("Keep the new-conversation draft")
                while remaining := page.locator(".delete-conversation-button").count():
                    page.locator(".delete-conversation-button").first.click()
                    expect(page.locator(".delete-conversation-button")).to_have_count(remaining - 1)
                expect(page.locator(".no-conversations")).to_have_text("No conversations yet")
                expect(page.locator("#message-input")).to_have_value(
                    "Keep the new-conversation draft"
                )
                assert page.request.get(base_url + "/api/conversations").json() == []
                print(
                    "PASS: trash buttons, active/background deletion, drafts, stopped runs",
                    flush=True,
                )
                check_projects(page, base_url, app)
                check_sidebar_scroll(page, base_url, app)
                assert all(url.startswith(base_url) for url in requests), requests
                assert not errors, errors
                print("PASS: mobile layout, local assets, no browser errors", flush=True)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
