"""Sidebar scrolling checks against the isolated browser test server."""

from playwright.sync_api import expect


def scroll_positions(page):
    return page.locator("#sidebar .sidebar-content, #sidebar .conversation-list").evaluate_all(
        "elements => elements.map(element => [element.scrollTop, element.scrollLeft])"
    )


def expect_scroll_positions(page, positions):
    expect(page.locator(".htmx-settling")).to_have_count(0)
    page.wait_for_function(
        """positions => [...document.querySelectorAll(
            '#sidebar .sidebar-content, #sidebar .conversation-list'
        )].every((element, index) => {
            const [top, left] = positions[index];
            return Math.abs(element.scrollTop - Math.min(top, element.scrollHeight - element.clientHeight)) < 1
                && Math.abs(element.scrollLeft - Math.min(left, element.scrollWidth - element.clientWidth)) < 1;
        })""",
        arg=positions,
    )


def check_sidebar_scroll(page, base_url, app):
    store = app.state.runs.store
    project_id = app.state.runs.projects.list()[0]["id"]
    for index in range(36):
        conversation = store.draft()
        conversation["title"] = f"Scroll {index:02}"
        if index < 18:
            conversation["project_id"] = project_id
        store.save(conversation)

    page.set_viewport_size({"width": 1440, "height": 800})
    page.goto(base_url)
    group = page.locator(f"#project-{project_id}")
    group.locator(":scope > summary").click()
    selected = group.locator(".conversation-row").nth(10)
    selected_id = selected.get_attribute("data-conversation-id")
    selected.scroll_into_view_if_needed()
    positions = scroll_positions(page)
    assert positions[0][0] > 0
    selected.locator(".conversation-item").click()
    expect(page).to_have_url(f"{base_url}/conversations/{selected_id}")
    expect(group).to_have_attribute("open", "")
    expect_scroll_positions(page, positions)

    # Deleting another conversation swaps just the sidebar.
    other = group.locator(".conversation-row").nth(11)
    other_id = other.get_attribute("data-conversation-id")
    other.locator(".delete-conversation-button").scroll_into_view_if_needed()
    positions = scroll_positions(page)
    other.locator(".delete-conversation-button").click()
    expect(page.locator(f'[data-conversation-id="{other_id}"].conversation-row')).to_have_count(0)
    expect(page).to_have_url(f"{base_url}/conversations/{selected_id}")
    expect_scroll_positions(page, positions)

    # Deleting the current conversation replaces the whole workspace.
    selected = page.locator(f'[data-conversation-id="{selected_id}"].conversation-row')
    selected.locator(".delete-conversation-button").scroll_into_view_if_needed()
    positions = scroll_positions(page)
    selected.locator(".delete-conversation-button").click()
    expect(page).to_have_url(base_url + "/")
    expect_scroll_positions(page, positions)

    # Live title/completion updates also replace the sidebar out of band.
    selected = group.locator(".conversation-item").nth(10)
    selected.click()
    expect(page.locator("#conversation")).not_to_have_attribute("data-conversation-id", "")
    positions = scroll_positions(page)
    page.locator("#message-input").fill("Keep the sidebar scrolled during a response")
    page.locator("#message-input").press("Enter")
    expect(page.locator(".assistant-message")).to_have_count(1)
    expect(page.locator("#message-input")).to_be_enabled()
    expect_scroll_positions(page, positions)

    # Removing the bottom row clamps to the new end of the list.
    last = page.locator("#ungrouped-conversations .conversation-row").last
    last_id = last.get_attribute("data-conversation-id")
    last.locator(".delete-conversation-button").scroll_into_view_if_needed()
    page.locator(".sidebar-content").evaluate("element => element.scrollTop = element.scrollHeight")
    positions = scroll_positions(page)
    last.locator(".delete-conversation-button").click()
    expect(page.locator(f'[data-conversation-id="{last_id}"].conversation-row')).to_have_count(0)
    expect_scroll_positions(page, positions)

    page.set_viewport_size({"width": 390, "height": 844})
    selected = page.locator("#ungrouped-conversations .conversation-row").nth(8)
    selected_id = selected.get_attribute("data-conversation-id")
    selected.locator(".conversation-item").scroll_into_view_if_needed()
    positions = scroll_positions(page)
    assert positions[0][0] > 0 and positions[1][1] > 0
    selected.locator(".conversation-item").click()
    expect(page).to_have_url(f"{base_url}/conversations/{selected_id}")
    expect_scroll_positions(page, positions)

    selected.locator(".delete-conversation-button").scroll_into_view_if_needed()
    positions = scroll_positions(page)
    selected.locator(".delete-conversation-button").click()
    expect(page).to_have_url(base_url + "/")
    expect_scroll_positions(page, positions)
    print(
        "PASS: sidebar scroll survives navigation, deletion and live updates on desktop/mobile",
        flush=True,
    )
