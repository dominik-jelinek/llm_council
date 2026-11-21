"""Project interaction checks run against the shared isolated browser test server."""

from playwright.sync_api import expect


def check_projects(page, base_url, app):
    page.set_viewport_size({"width": 1440, "height": 1050})
    section = page.locator("#projects-section")
    expect(section).to_have_attribute("open", "")
    for name in ("Research", "Writing"):
        page.get_by_role("button", name="New project", exact=True).click()
        dialog = page.get_by_role("dialog", name="New project", exact=True)
        expect(dialog).to_be_visible()
        dialog.get_by_label("Project name", exact=True).fill(name)
        dialog.get_by_role("button", name="Create project", exact=True).click()
        expect(dialog).not_to_be_visible()
    expect(page.locator(".project-group")).to_have_count(2)
    expect(page.locator(".project-group[open]")).to_have_count(0)
    assert section.bounding_box()["y"] >= page.locator(".sidebar-header").bounding_box()["height"]
    assert (
        section.bounding_box()["y"] < page.locator("#ungrouped-conversations").bounding_box()["y"]
    )

    research, writing = page.locator(".project-group").nth(0), page.locator(".project-group").nth(1)
    research_id = research.get_attribute("data-project-drop-target")
    writing_id = writing.get_attribute("data-project-drop-target")
    section.locator(":scope > summary").click()
    expect(section).not_to_have_attribute("open", "")
    page.get_by_role("button", name="+ New Conversation").click()
    expect(section).not_to_have_attribute("open", "")
    section.locator(":scope > summary").click()

    page.get_by_role("button", name="New conversation in Research", exact=True).click()
    expect(page).to_have_url(f"{base_url}/?project={research_id}")
    expect(page.locator("#message-input")).to_have_value("")
    page.locator("#message-input").fill("Research draft\nSecond line")
    page.get_by_role("button", name="New conversation in Writing", exact=True).click()
    expect(page.locator("#message-input")).to_have_value("")
    page.locator("#message-input").fill("Writing draft")
    page.get_by_role("button", name="New conversation in Research", exact=True).click()
    expect(page.locator("#message-input")).to_have_value("Research draft\nSecond line")
    assert page.request.get(base_url + "/api/conversations").json() == []
    research.locator(":scope > summary").click()
    expect(research).to_have_attribute("open", "")
    page.locator("#message-input").fill("A research question")
    page.locator("#message-input").press("Enter")
    expect(page.locator(".user-message")).to_have_count(1)
    research_conversation_url = page.url
    research_conversation_id = page.url.rsplit("/", 1)[1]
    expect(page.locator("#message-input")).to_be_enabled()
    expect(research).to_have_attribute("open", "")
    expect(research.locator(".conversation-item")).to_have_count(1)
    expect(page.locator("#ungrouped-conversations .conversation-item")).to_have_count(0)
    assert (
        page.request.get(f"{base_url}/api/conversations/{research_conversation_id}").json()[
            "project_id"
        ]
        == research_id
    )

    page.locator("#message-input").fill("Keep this research follow-up")
    research.locator(".conversation-menu-button").click()
    dialog = page.get_by_role("dialog", name="Move conversation", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label("Project", exact=True)).to_have_value(research_id)
    dialog.get_by_label("Project", exact=True).select_option(writing_id)
    runs_before_move = len(app.state.runs.runs)
    dialog.get_by_role("button", name="Move", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(research.locator(".conversation-item")).to_have_count(0)
    expect(writing.locator(".conversation-item")).to_have_count(1)
    expect(writing).not_to_have_attribute("open", "")
    expect(page).to_have_url(research_conversation_url)
    expect(page.locator("#message-input")).to_have_value("Keep this research follow-up")
    assert len(app.state.runs.runs) == runs_before_move

    writing.locator(":scope > summary").click()
    research.locator(":scope > summary").click()
    expect(research).not_to_have_attribute("open", "")
    writing.locator(".conversation-row").drag_to(research.locator(":scope > summary"))
    expect(research.locator(".conversation-item")).to_have_count(1)
    expect(writing.locator(".conversation-item")).to_have_count(0)
    expect(research).not_to_have_attribute("open", "")
    research.locator(":scope > summary").click()
    research.locator(".conversation-row").drag_to(page.locator(".conversations-heading"))
    expect(page.locator("#ungrouped-conversations .conversation-item")).to_have_count(1)
    expect(research.locator(".conversation-item")).to_have_count(0)
    expect(page.locator("#message-input")).to_have_value("Keep this research follow-up")
    assert (
        page.request.get(f"{base_url}/api/conversations/{research_conversation_id}").json()[
            "project_id"
        ]
        is None
    )

    page.get_by_role("button", name="New conversation in Writing", exact=True).click()
    expect(page.locator("#message-input")).to_have_value("Writing draft")
    page.locator("#message-input").fill("An independent writing question")
    page.locator("#message-input").press("Enter")
    expect(page.locator(".user-message")).to_have_count(1)
    writing_conversation_id = page.url.rsplit("/", 1)[1]
    expect(page.locator("#message-input")).to_be_enabled()
    expect(writing.locator(".conversation-item")).to_have_count(1)
    page.screenshot(path="/tmp/llm-council-projects-desktop.png")
    page.reload()
    expect(page.locator(".project-group[open]")).to_have_count(0)
    expect(writing.locator(".conversation-item")).to_have_count(1)
    expect(page.locator(".project-group")).to_have_count(2)

    page.set_viewport_size({"width": 390, "height": 844})
    writing.locator(":scope > summary").click()
    writing.locator(".conversation-menu-button").click()
    expect(dialog).to_be_visible()
    assert not dialog.evaluate("element => element.scrollWidth > element.clientWidth")
    dialog.get_by_label("Project", exact=True).select_option("")
    dialog.get_by_role("button", name="Move", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.locator("#ungrouped-conversations .conversation-item")).to_have_count(2)
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.screenshot(path="/tmp/llm-council-projects-mobile.png")
    page.locator(f'form[action="/conversations/{writing_conversation_id}/delete"] button').click()
    expect(page.locator("#empty-state")).to_be_visible()
    expect(page.locator("#message-input")).to_have_value("Keep the new-conversation draft")
    expect(page.locator(".project-group")).to_have_count(2)
    assert len(page.request.get(base_url + "/api/conversations").json()) == 1
    print(
        "PASS: projects, scoped drafts, membership, drag-and-drop, collapse state and mobile menu",
        flush=True,
    )
