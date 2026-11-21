/* Keep navigation in place when HTMX replaces the sidebar or workspace. */
(() => {
  const positions = new Map();
  const selectors = ['#sidebar .sidebar-content', '#sidebar .conversation-list'];

  function rememberScroll(event) {
    const sidebar = document.querySelector('#sidebar');
    if (!sidebar || !event.detail.target?.contains(sidebar)) return;
    // Capture at swap time so scrolling while a request is pending is retained.
    selectors.forEach((selector) => {
      const element = document.querySelector(selector);
      if (element) positions.set(selector, { element, top: element.scrollTop, left: element.scrollLeft });
    });
  }

  function restoreScroll() {
    positions.forEach((position, selector) => {
      const element = document.querySelector(selector);
      if (!element || element === position.element) return;
      element.scrollTop = position.top;
      element.scrollLeft = position.left;
      positions.delete(selector);
    });
  }

  document.addEventListener('htmx:beforeSwap', rememberScroll);
  document.addEventListener('htmx:oobBeforeSwap', rememberScroll);
  // projects.js restores expanded groups first, making the full scroll range available.
  document.addEventListener('htmx:afterSwap', restoreScroll);
  document.addEventListener('htmx:oobAfterSwap', restoreScroll);
  document.addEventListener('htmx:afterSettle', restoreScroll);
})();
