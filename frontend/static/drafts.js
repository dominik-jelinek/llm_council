/* Unsent questions belong to this tab, not the saved conversation history. */
(() => {
  const drafts = new Map();
  const submissions = new WeakMap();
  let restoredInput = null;

  function keyFor(input) {
    return `llm-council:draft:${input.form.dataset.draftKey}`;
  }

  function readDraft(key) {
    if (!drafts.has(key)) {
      try {
        drafts.set(key, sessionStorage.getItem(key) || '');
      } catch {
        drafts.set(key, '');
      }
    }
    return drafts.get(key);
  }

  function writeDraft(key, text) {
    drafts.set(key, text);
    try {
      if (text) sessionStorage.setItem(key, text);
      else sessionStorage.removeItem(key);
    } catch {
      // Navigation within this page still preserves drafts when storage is unavailable.
    }
  }

  function restoreDraft() {
    const input = document.querySelector('#message-input');
    if (!input || input === restoredInput) return;
    input.value = readDraft(keyFor(input));
    restoredInput = input;
  }

  document.addEventListener('input', (event) => {
    if (event.target.matches('#message-input')) {
      writeDraft(keyFor(event.target), event.target.value);
    }
  });

  document.addEventListener('htmx:beforeRequest', (event) => {
    if (!event.detail.elt.matches('.input-form')) return;
    const input = event.detail.elt.elements.content;
    const key = keyFor(input);
    writeDraft(key, input.value);
    submissions.set(event.detail.xhr, { key, text: input.value });
  });

  document.addEventListener('htmx:beforeSwap', (event) => {
    const { xhr } = event.detail;
    const submitted = submissions.get(xhr);
    if (!submitted || xhr.status < 200 || xhr.status >= 300) return;
    // Clear before the replacement composer restores its draft, and never erase a newer edit.
    if (readDraft(submitted.key) === submitted.text) writeDraft(submitted.key, '');
    submissions.delete(xhr);
  });

  document.addEventListener('htmx:afterSwap', restoreDraft);
  document.addEventListener('htmx:oobAfterSwap', restoreDraft);
  document.addEventListener('htmx:afterSettle', restoreDraft);
  document.addEventListener('conversationDeleted', (event) => {
    writeDraft(`llm-council:draft:${event.detail.value}`, '');
  });
  restoreDraft();
})();
