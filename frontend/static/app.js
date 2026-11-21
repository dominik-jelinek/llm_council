/* Local interactions only. HTMX owns requests and server-rendered updates. */
(() => {
  let followLatest = true;
  let focusedMessage = null;
  const stageStates = new Map();

  function resizeComposer(input) {
    const styles = getComputedStyle(input);
    const border = parseFloat(styles.borderTopWidth) + parseFloat(styles.borderBottomWidth);
    const scrollTop = input.scrollTop;
    input.style.height = 'auto';
    input.style.overflowY = 'hidden';
    input.style.height = `${Math.min(input.scrollHeight + border, parseFloat(styles.maxHeight))}px`;
    input.style.overflowY = input.scrollHeight > input.clientHeight ? 'auto' : 'hidden';
    input.scrollTop = scrollTop;
  }

  function updateComposer() {
    const input = document.querySelector('#message-input');
    if (!input) return;
    resizeComposer(input);
    const button = input.form.querySelector('button[type="submit"]');
    button.disabled = input.disabled || !input.value.trim();
    const busy = document.querySelector('#composer').dataset.busy === 'true';
    document.querySelectorAll('.edit-message-form').forEach((form) => {
      form.querySelector('button[type="submit"]').disabled = busy || !form.elements.content.value.trim();
    });
  }

  function scrollToLatest() {
    const messages = document.querySelector('#messages');
    const message = focusedMessage && document.getElementById(`message-${focusedMessage}`);
    if (messages && message) {
      messages.scrollTop += message.getBoundingClientRect().top - messages.getBoundingClientRect().top - 16;
    } else if (messages && followLatest) messages.scrollTop = messages.scrollHeight;
  }

  function selectTab(tab, focus = false) {
    const group = tab.closest('.model-tabs');
    for (const sibling of group.querySelectorAll('[role="tab"]')) {
      const selected = sibling === tab;
      sibling.setAttribute('aria-selected', String(selected));
      sibling.tabIndex = selected ? 0 : -1;
      sibling.classList.toggle('active', selected);
      document.getElementById(sibling.getAttribute('aria-controls')).hidden = !selected;
    }
    if (focus) tab.focus();
  }

  async function writeClipboard(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // Plain HTTP on a local network may not expose the Clipboard API.
    const previousFocus = document.activeElement;
    const input = document.createElement('textarea');
    input.className = 'clipboard-fallback';
    input.value = text;
    input.readOnly = true;
    document.body.append(input);
    try {
      input.select();
      if (!document.execCommand('copy')) throw new Error('Copy unavailable');
    } finally {
      input.remove();
      previousFocus?.focus({ preventScroll: true });
    }
  }

  async function copyMessage(button) {
    const control = button.closest('.copy-control');
    const status = control.querySelector('.copy-status');
    try {
      await writeClipboard(control.querySelector('.copy-source').textContent);
      status.textContent = 'Copied!';
      setTimeout(() => {
        if (status.textContent === 'Copied!') status.textContent = '';
      }, 2000);
    } catch {
      status.textContent = 'Copy failed. Select the text and copy manually.';
    }
  }

  function updateCouncilEditor() {
    const rows = document.querySelectorAll('#council-members .member-row');
    rows.forEach((row) => { row.querySelector('.remove-member').disabled = rows.length <= 1; });
    const add = document.querySelector('#add-member');
    if (add) add.disabled = rows.length >= 26;
  }

  document.addEventListener('click', (event) => {
    const usage = event.target.closest('.usage-details-button');
    if (usage) {
      const template = usage.closest('.usage-footer').querySelector('.usage-template');
      document.querySelector('#usage-dialog-content').replaceChildren(template.content.cloneNode(true));
      document.querySelector('#usage-dialog').showModal();
    }
    const close = event.target.closest('[data-close-dialog]');
    const dialog = close?.closest('dialog');
    if (dialog) {
      event.preventDefault();
      dialog.close();
    }
    if (event.target.closest('#add-member')) {
      const members = document.querySelector('#council-members');
      members.append(document.querySelector('#member-template').content.cloneNode(true));
      members.lastElementChild.querySelector('input').focus();
      updateCouncilEditor();
    }
    const remove = event.target.closest('.remove-member');
    if (remove) {
      remove.closest('.member-row').remove();
      updateCouncilEditor();
    }
    const tab = event.target.closest('[role="tab"]');
    if (tab) selectTab(tab);
    const copy = event.target.closest('.copy-button');
    if (copy) copyMessage(copy);
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('#message-input') && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!event.target.disabled && event.target.value.trim()) event.target.form.requestSubmit();
    }
    const tab = event.target.closest('[role="tab"]');
    if (!tab || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]')];
    const offset = event.key === 'ArrowRight' ? 1 : -1;
    let index = (tabs.indexOf(tab) + offset + tabs.length) % tabs.length;
    if (event.key === 'Home') index = 0;
    if (event.key === 'End') index = tabs.length - 1;
    selectTab(tabs[index], true);
  });

  document.addEventListener('input', (event) => {
    if (event.target.matches('#message-input, .edit-content')) updateComposer();
    if (event.target.matches('#message-input')) scrollToLatest();
    if (event.target.closest('#council-settings')) document.querySelector('.setup-success')?.remove();
  });

  window.addEventListener('resize', () => {
    updateComposer();
    scrollToLatest();
  });

  document.addEventListener('scroll', (event) => {
    if (event.target.id !== 'messages') return;
    const element = event.target;
    followLatest = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
  }, true);

  document.addEventListener('htmx:beforeRequest', (event) => {
    if (event.detail.elt.matches('.input-form, .retry-form')) {
      const error = document.querySelector('#request-error');
      if (error) error.replaceChildren();
      focusedMessage = null;
      followLatest = true;
    }
  });

  document.addEventListener('htmx:beforeSwap', (event) => {
    // Expected server validation errors are HTML intended for the inline notice.
    const target = event.detail.xhr.getResponseHeader('HX-Retarget');
    if (['#request-error', '#council-settings', '#project-editor'].includes(target) || target?.startsWith('#edit-error-')) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  document.addEventListener('htmx:afterSwap', (event) => {
    if (event.target.id === 'workspace') {
      stageStates.clear();
      focusedMessage = null;
      followLatest = true;
    }
    event.target.querySelectorAll('details.stage').forEach((stage) => {
      if (stageStates.has(stage.id)) stage.open = stageStates.get(stage.id);
    });
    if (event.target.matches('.message-group')) {
      const editor = event.target.querySelector('.edit-content');
      if (editor) {
        followLatest = false;
        editor.focus({ preventScroll: true });
        editor.setSelectionRange(editor.value.length, editor.value.length);
      }
    }
    if (event.target.id === 'council-settings') {
      const dialog = document.querySelector('#council-dialog');
      if (dialog && !dialog.open) dialog.showModal();
      updateCouncilEditor();
    }
    updateComposer();
    scrollToLatest();
  });

  document.addEventListener('focusMessage', (event) => {
    focusedMessage = event.detail.value;
    followLatest = false;
    scrollToLatest();
  });

  document.addEventListener('htmx:afterSettle', () => {
    updateComposer();
    scrollToLatest();
    focusedMessage = null;
  });

  document.addEventListener('htmx:sendError', () => {
    const error = document.querySelector('#request-error');
    if (error) error.textContent = 'Could not reach the server. Reopen the conversation to check its status before retrying.';
  });

  document.addEventListener('htmx:responseError', () => {
    const error = document.querySelector('#request-error');
    if (error) error.textContent = 'The request could not be completed. Reopen the conversation to check its status.';
  });

  function connectionNotice(event, visible) {
    const notice = event.detail.elt.querySelector('.connection-status');
    if (notice) notice.hidden = !visible;
  }
  document.addEventListener('htmx:sseError', (event) => connectionNotice(event, true));
  document.addEventListener('htmx:sseOpen', (event) => connectionNotice(event, false));
  document.addEventListener('htmx:sseBeforeMessage', (event) => {
    event.detail.elt.querySelectorAll('details.stage').forEach((stage) => {
      stageStates.set(stage.id, stage.open);
    });
  });
  document.addEventListener('htmx:sseMessage', () => {
    updateComposer();
    scrollToLatest();
  });
  document.addEventListener('htmx:sseClose', (event) => {
    event.detail.elt.querySelectorAll('.stage-loading').forEach((element) => {
      element.closest('details.stage')?.remove();
    });
    connectionNotice(event, false);
    updateComposer();
  });

  updateComposer();
  updateCouncilEditor();
  scrollToLatest();
})();
