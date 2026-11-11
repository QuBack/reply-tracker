document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('form[method="post"]').forEach(form => {
  form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    if (form.matches('[data-purchase-wizard]') && form.dataset.wizardReady !== 'true') return;
    const button = event.submitter;
    if (button) {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.textContent = 'Подождите…';
    }
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('button[aria-busy]').forEach(button => {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    // Restore form controls when navigating back from a validation page.
    if (performance.getEntriesByType('navigation')[0]?.type === 'back_forward') location.reload();
  });
});
document.querySelectorAll('[data-back]').forEach(button => button.addEventListener('click', () => history.back()));
const jobs = document.querySelector('[data-jobs]');
if (jobs?.dataset.active === '1') {
  const snapshot = [...jobs.querySelectorAll('[data-job-id]')].map(row => `${row.dataset.jobId}:${row.dataset.jobState}`).join('|');
  const poll = async () => {
    if (document.hidden) return setTimeout(poll, 5000);
    try {
      const response = await fetch('/api/jobs', { credentials: 'same-origin' });
      if (response.status === 401) return location.assign('/login');
      if (!response.ok) throw new Error('poll');
      const rows = await response.json();
      if (rows.map(row => `${row.id}:${row.state}`).join('|') !== snapshot) return location.reload();
    } catch {
      const status = document.querySelector('[data-poll-status]');
      if (status) status.textContent = 'Нет связи. Повторяем подключение…';
    }
    setTimeout(poll, 5000);
  };
  setTimeout(poll, 5000);
}

// Keep progress on the screen where the user started the operation.
const operation = document.querySelector('[data-operation-id]');
if (operation && ['queued', 'running'].includes(operation.dataset.operationState)) {
  const pollOperation = async () => {
    if (document.hidden) return setTimeout(pollOperation, 4000);
    try {
      const response = await fetch('/api/jobs', {credentials: 'same-origin'});
      if (response.status === 401) return location.assign('/login');
      if (!response.ok) throw new Error('poll');
      const rows = await response.json();
      const row = rows.find(item => item.id === operation.dataset.operationId);
      if (row && row.state !== operation.dataset.operationState) return location.reload();
    } catch {
      // Keep the user's form and page intact while connectivity recovers.
    }
    setTimeout(pollOperation, 4000);
  };
  setTimeout(pollOperation, 4000);
}

const wizard = document.querySelector('[data-purchase-wizard]');
if (wizard) {
  const panels = [...wizard.querySelectorAll('[data-wizard-step]')];
  const indicators = [...wizard.querySelectorAll('[data-step-indicator]')];
  const next = wizard.querySelector('[data-wizard-next]');
  const back = wizard.querySelector('[data-wizard-back]');
  const cancel = wizard.querySelector('[data-wizard-cancel]');
  const save = wizard.querySelector('[data-wizard-save]');
  const error = wizard.querySelector('[data-wizard-error]');
  const boxes = [...wizard.querySelectorAll('input[name="suppliers"]')];
  let step = 0;
  const selected = () => boxes.filter(box => box.checked && !box.disabled);
  const updateCount = () => {
    wizard.querySelector('[data-picker-count]').textContent = `Выбрано: ${selected().length}`;
  };
  const review = () => {
    const value = name => wizard.elements.namedItem(name).value;
    const write = (name, text) => wizard.querySelector(`[data-review-${name}]`).textContent = text;
    write('name', value('name'));
    write('subject', value('subject'));
    write('body', value('body'));
    write('deadline', value('deadline') ? value('deadline').split('-').reverse().join('.') : 'Без ограничения');
    write('suppliers', selected().map(box => box.dataset.supplierName).join(', '));
    const existing = wizard.querySelector('[data-review-files]').dataset.existingFiles;
    const files = [...wizard.querySelectorAll('input[type="file"]')].flatMap(input => [...input.files].map(file => file.name));
    write('files', [existing, ...files].filter(Boolean).join(', ') || 'Без вложений');
  };
  const render = (focus = false) => {
    error.hidden = true;
    panels.forEach((panel, index) => panel.hidden = index !== step);
    indicators.forEach((indicator, index) => {
      indicator.classList.toggle('current', index === step);
      indicator.classList.toggle('completed', index < step);
      if (index === step) indicator.setAttribute('aria-current', 'step');
      else indicator.removeAttribute('aria-current');
    });
    back.hidden = step === 0;
    cancel.hidden = step !== 0;
    next.hidden = step === panels.length - 1;
    save.hidden = step !== panels.length - 1;
    wizard.dataset.wizardReady = String(step === panels.length - 1);
    next.textContent = step === 0 ? 'Далее: поставщики' : 'Далее: проверить заявку';
    if (step === panels.length - 1) review();
    if (focus) {
      const legend = panels[step].querySelector('legend');
      legend.tabIndex = -1;
      legend.focus({preventScroll: true});
      panels[step].scrollIntoView({block: 'start'});
    }
  };
  const advance = () => {
    const invalid = [...panels[step].querySelectorAll('input, textarea, select')].find(input => !input.checkValidity());
    if (invalid) {
      const details = invalid.closest('details');
      if (details) details.open = true;
      invalid.reportValidity();
      return;
    }
    if (step === 1 && !selected().length) {
      error.textContent = 'Выберите хотя бы одного поставщика с email.';
      error.hidden = false;
      error.scrollIntoView({block: 'center'});
      return;
    }
    const totalSize = [...wizard.querySelectorAll('input[type="file"]')].flatMap(input => [...input.files]).reduce((sum,file) => sum + file.size, 0);
    if (totalSize > 25 * 1024 * 1024) {
      error.textContent = 'Общий размер файлов должен быть не больше 25 МБ.';
      error.hidden = false;
      return;
    }
    if (step < panels.length - 1) {
      step += 1;
      render(true);
    }
  };
  next.addEventListener('click', advance);
  back.addEventListener('click', () => {step -= 1; render(true);});
  wizard.addEventListener('submit', event => {
    if (step !== panels.length - 1) {
      event.preventDefault();
      advance();
    }
  });
  boxes.forEach(box => box.addEventListener('change', updateCount));
  wizard.querySelector('[data-picker-search]').addEventListener('input', event => {
    const query = event.target.value.trim().toLocaleLowerCase('ru');
    wizard.querySelectorAll('[data-picker-option]').forEach(option => {
      option.hidden = !option.textContent.toLocaleLowerCase('ru').includes(query);
    });
  });
  updateCount();
  render();
}
