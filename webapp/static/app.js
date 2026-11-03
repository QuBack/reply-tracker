document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('form[method="post"]').forEach(form => {
  form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
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
