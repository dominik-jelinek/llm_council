/* Project disclosure state and drag-and-drop; HTMX handles persistence and rendering. */
(() => {
  const states = new Map();
  const restored = new WeakSet();
  let draggedConversation = null;

  function restoreProjects() {
    document.querySelectorAll('.project-disclosure').forEach((details) => {
      if (restored.has(details)) return;
      if (states.has(details.id)) details.open = states.get(details.id);
      restored.add(details);
    });
  }

  document.addEventListener('toggle', (event) => {
    if (event.target.matches('.project-disclosure') && event.target.isConnected) {
      states.set(event.target.id, event.target.open);
    }
  }, true);

  document.addEventListener('htmx:afterSwap', (event) => {
    restoreProjects();
    if (event.target.id === 'project-editor') {
      const dialog = document.querySelector('#project-dialog');
      if (dialog && !dialog.open) dialog.showModal();
      event.target.querySelector('input:not([type="hidden"]), select')?.focus();
    }
  });
  document.addEventListener('htmx:oobAfterSwap', restoreProjects);
  document.addEventListener('htmx:afterSettle', restoreProjects);
  document.addEventListener('projectEditorSaved', () => {
    document.querySelector('#project-dialog')?.close();
  });

  function clearDropTargets() {
    document.querySelectorAll('.drag-over').forEach((target) => target.classList.remove('drag-over'));
  }

  document.addEventListener('dragstart', (event) => {
    const row = event.target.closest('.conversation-row');
    if (!row) return;
    draggedConversation = row.dataset.conversationId;
    event.dataTransfer.setData('application/x-council-conversation', draggedConversation);
    event.dataTransfer.effectAllowed = 'move';
  });
  document.addEventListener('dragend', () => {
    draggedConversation = null;
    clearDropTargets();
  });
  document.addEventListener('dragover', (event) => {
    const target = event.target.closest('[data-project-drop-target]');
    if (!draggedConversation || !target) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    clearDropTargets();
    target.classList.add('drag-over');
  });
  document.addEventListener('dragleave', (event) => {
    const target = event.target.closest('[data-project-drop-target]');
    if (target && !target.contains(event.relatedTarget)) target.classList.remove('drag-over');
  });
  document.addEventListener('drop', (event) => {
    const target = event.target.closest('[data-project-drop-target]');
    if (!draggedConversation || !target) return;
    event.preventDefault();
    const values = { project_id: target.dataset.projectDropTarget };
    document.querySelectorAll('#sidebar-context input').forEach((input) => {
      values[input.name] = input.value;
    });
    htmx.ajax('POST', `/conversations/${draggedConversation}/project`, {
      source: document.querySelector('#sidebar'), target: '#sidebar', swap: 'outerHTML', values,
    }).catch(() => {}); // HTMX's shared error handlers display request failures.
    draggedConversation = null;
    clearDropTargets();
  });

  restoreProjects();
})();
