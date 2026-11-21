# Frontend

FastAPI serves this directory directly:

- `templates/`: Jinja HTML pages and reusable fragments.
- `static/app.css`: layout and components.
- `static/markdown.css`: rendered message formatting.
- `static/app.js`: tabs, copying, keyboard shortcuts, scrolling, and request feedback.
- `static/drafts.js`: per-conversation composer drafts in tab-local browser storage.
- `static/projects.js`: project collapse state, assignment dialog, and drag-and-drop.
- `static/sidebar-scroll.js`: sidebar scroll position across navigation and updates.
- `static/vendor/`: pinned HTMX and SSE extension, including licenses.

There is no build step. Start the application from the project root with
`./start.sh`, then open http://localhost:8001.

HTMX submits forms and swaps server-rendered HTML. Each run opens its own
read-only SSE connection; stage updates target only that stage. Sidebar and
composer changes use out-of-band swaps. Navigation closes the old subscription,
while the server-owned task continues.

Run `uv run --with playwright python -m tests.browser_smoke` from the project
root for the browser checks. See the root README for setup.
