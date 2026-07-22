(() => {
  'use strict';

  const THEME_KEY = 'panel-v2-theme';
  const SECTION_KEY = 'panel-v2-section';
  const ACCOUNT_PAGE_SIZE_KEY = 'panel-v2-account-page-size';
  const DEFAULT_SECTION_HASH = '#overview';
  const THEMES = new Set(['system', 'light', 'dark']);
  const SECTIONS = new Set([
    'overview',
    'register',
    'accounts',
    'mail',
    'credentials',
    'logs',
  ]);
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const state = {
    job: null,
    browser: null,
    credentials: null,
    email: null,
    busy: new Set(),
    pollTimer: null,
    confirmResolver: null,
    confirmFocus: null,
    accounts: {
      loaded: false,
      loading: false,
      page: 1,
      pageSize: 25,
      q: '',
      source: 'all',
      status: 'all',
      sort: 'newest',
      totalPages: 0,
      files: [],
      selectedFiles: new Set(),
      controller: null,
      requestGeneration: 0,
      searchTimer: null,
    },
  };
  const workerControlPending = new Set();

  function readPreference(key, fallback) {
    try {
      return window.localStorage.getItem(key) || fallback;
    } catch (_) {
      return fallback;
    }
  }

  function savePreference(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (_) {}
  }

  function resolvedTheme(preference) {
    if (preference === 'system') return systemTheme.matches ? 'dark' : 'light';
    return preference;
  }

  function applyTheme(preference, persist = true) {
    const next = THEMES.has(preference) ? preference : 'system';
    document.documentElement.dataset.themePreference = next;
    document.documentElement.dataset.theme = resolvedTheme(next);
    const select = document.getElementById('theme-toggle');
    if (select) select.value = next;
    if (persist) savePreference(THEME_KEY, next);
  }

  function requestedSection() {
    const hash = window.location.hash.toLowerCase();
    const fromHash = hash.startsWith('#') ? hash.slice(1) : hash;
    if (SECTIONS.has(fromHash)) return fromHash;
    const stored = readPreference(SECTION_KEY, DEFAULT_SECTION_HASH.slice(1));
    return SECTIONS.has(stored) ? stored : 'overview';
  }

  function showSection(name, updateHash = false) {
    const next = SECTIONS.has(name) ? name : 'overview';
    document.querySelectorAll('[data-section]').forEach((section) => {
      section.hidden = section.dataset.section !== next;
    });
    document.querySelectorAll('[data-section-link]').forEach((link) => {
      if (link.dataset.sectionLink === next) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
    savePreference(SECTION_KEY, next);
    if (updateHash && window.location.hash !== `#${next}`) {
      window.history.pushState(null, '', `#${next}`);
    }
    if (next === 'accounts') ensureAccountsLoaded();
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value ?? '');
  }

  function setInlineError(id, message = '') {
    const element = document.getElementById(id);
    if (!element) return;
    element.textContent = message;
    element.hidden = !message;
  }

  function safeErrorMessage(error) {
    const value = error instanceof Error ? error.message : String(error || '请求失败');
    return value.slice(0, 500);
  }

  function showToast(message, tone = 'info') {
    const region = document.getElementById('toast-region');
    if (!region) return;
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.dataset.tone = tone;
    toast.textContent = message;
    region.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  async function requestJson(path, options = {}) {
    const init = {
      method: options.method || 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(options.headers || {}) },
      signal: options.signal,
    };
    if (options.body instanceof FormData) {
      init.body = options.body;
    } else if (options.body !== undefined) {
      init.body = JSON.stringify(options.body);
      init.headers['Content-Type'] = 'application/json';
    }
    const response = await window.fetch(path, init);
    const raw = await response.text();
    let payload = {};
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch (_) {
      payload = {};
    }
    if (!response.ok || payload.ok === false) {
      const message = typeof payload.error === 'string'
        ? payload.error
        : `请求失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return payload;
  }

  function setBusy(group, active) {
    if (active) state.busy.add(group);
    else state.busy.delete(group);
    document.querySelectorAll(`[data-busy-group="${group}"]`).forEach((control) => {
      control.disabled = active;
    });
    if (group === 'registration') syncRegistrationControls();
    if (group === 'accounts') syncAccountControls();
  }

  function confirmAction({ title, message, acceptLabel = '确认' }) {
    const dialog = document.getElementById('confirm-dialog');
    if (!dialog) return Promise.resolve(false);
    if (state.confirmResolver) state.confirmResolver(false);
    state.confirmFocus = document.activeElement;
    setText('confirm-title', title);
    setText('confirm-message', message);
    setText('confirm-accept', acceptLabel);
    dialog.returnValue = 'cancel';
    return new Promise((resolve) => {
      state.confirmResolver = resolve;
      dialog.addEventListener('close', () => {
        const accepted = dialog.returnValue === 'accept';
        const resolver = state.confirmResolver;
        state.confirmResolver = null;
        resolver?.(accepted);
        state.confirmFocus?.focus?.();
        state.confirmFocus = null;
      }, { once: true });
      dialog.showModal();
    });
  }

  function createElement(tag, className = '', text = '') {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== '') element.textContent = text;
    return element;
  }

  function statusLabel(value) {
    const labels = {
      idle: '空闲',
      starting: '启动中',
      running: '运行中',
      completed: '已完成',
      failed: '失败',
      stopped: '已停止',
    };
    return labels[value] || value || '空闲';
  }

  function syncRegistrationControls() {
    const running = Boolean(state.job?.running);
    const busy = state.busy.has('registration');
    const engine = document.getElementById('browser-engine')?.value || 'chromium';
    for (const id of ['register-count', 'register-concurrency', 'browser-engine']) {
      const control = document.getElementById(id);
      if (control) control.disabled = running || busy;
    }
    const windowMode = document.getElementById('browser-window-mode');
    if (windowMode) windowMode.disabled = running || busy || engine === 'camoufox';
    const save = document.getElementById('save-browser-settings');
    const start = document.getElementById('start-registration');
    const stop = document.getElementById('stop-registration');
    if (save) save.disabled = running || busy;
    if (start) start.disabled = running || busy;
    if (stop) stop.disabled = !running || busy;
  }

  function renderWorkers(workers = []) {
    const container = document.getElementById('worker-grid');
    if (!container) return;
    container.replaceChildren();
    setText('worker-count', `${workers.length} 个 Worker`);
    if (!workers.length) {
      const empty = createElement('div', 'empty-state worker-empty');
      const icon = createElement('span', '', '□');
      icon.setAttribute('aria-hidden', 'true');
      empty.append(
        icon,
        createElement('strong', '', '暂无 Worker'),
        createElement('p', '', '任务启动后，每个并发槽会显示在这里。'),
      );
      container.append(empty);
      return;
    }

    workers.forEach((worker) => {
      const browser = worker.browser || {};
      const workerId = Number(worker.worker_id || 0);
      const generation = Number(browser.generation || 0);
      const pendingKey = `${workerId}:${generation}`;
      const card = document.createElement('article');
      card.className = 'worker-card';
      card.dataset.workerId = String(workerId);

      const header = createElement('div', 'worker-card__header');
      const title = createElement('div', 'worker-card__title');
      title.append(
        createElement('strong', '', `Worker ${workerId}`),
        createElement('small', '', `轮次 ${worker.start_index || '—'} · 批量 ${worker.batch_count || 0}`),
      );
      const workerState = document.createElement('span');
      workerState.className = 'worker-card__state';
      workerState.textContent = statusLabel(worker.status);
      header.append(title, workerState);

      const meta = createElement('div', 'worker-card__meta');
      meta.append(
        createElement('span', '', `PID ${browser.pid || worker.pid || '—'}`),
        createElement('span', '', `窗口 ${browser.state || '不可用'}`),
      );

      const actions = createElement('div', 'worker-card__actions');
      const available = Number(browser.pid || 0) > 0 && Number(browser.hwnd || 0) > 0;
      if (available) {
        const action = browser.state === 'visible' ? 'hide' : 'show';
        const button = createElement(
          'button',
          `button ${action === 'hide' ? 'button--ghost' : 'button--secondary'}`,
          action === 'hide' ? '隐藏浏览器' : '显示浏览器',
        );
        button.type = 'button';
        button.disabled = workerControlPending.has(pendingKey);
        button.addEventListener('click', () => controlWorkerBrowser(worker, action));
        actions.append(button);
      } else {
        actions.append(createElement('span', 'status-label', '浏览器窗口不可用'));
      }

      card.append(header, meta, actions);
      container.append(card);
    });
  }

  async function controlWorkerBrowser(worker, action) {
    const browser = worker.browser || {};
    const workerId = Number(worker.worker_id || 0);
    const pendingKey = `${workerId}:${Number(browser.generation || 0)}`;
    if (!workerId || workerControlPending.has(pendingKey)) return;
    workerControlPending.add(pendingKey);
    renderWorkers(state.job?.workers || []);
    try {
      await requestJson(`/api/job/workers/${workerId}/browser/${action}`, { method: 'POST' });
      showToast(action === 'show' ? `Worker ${workerId} 浏览器已显示` : `Worker ${workerId} 浏览器已隐藏`);
      await loadJobStatus({ silent: true });
    } catch (error) {
      showToast(safeErrorMessage(error), 'error');
    } finally {
      workerControlPending.delete(pendingKey);
      renderWorkers(state.job?.workers || []);
    }
  }

  function setAccountsError(message = '') {
    const alert = document.getElementById('accounts-error');
    if (!alert) return;
    setText('accounts-error-text', message);
    alert.hidden = !message;
  }

  function syncAccountControls() {
    const accounts = state.accounts;
    const busy = accounts.loading || state.busy.has('accounts');
    const previous = document.getElementById('accounts-prev');
    const next = document.getElementById('accounts-next');
    const remove = document.getElementById('account-files-delete');
    const selectAll = document.getElementById('account-files-select-all');
    if (previous) previous.disabled = busy || accounts.page <= 1;
    if (next) next.disabled = busy || accounts.totalPages === 0 || accounts.page >= accounts.totalPages;
    if (remove) remove.disabled = busy || accounts.selectedFiles.size === 0;
    if (selectAll) selectAll.disabled = busy || accounts.files.length === 0;
  }

  function updateAccountSources(sources = []) {
    const select = document.getElementById('accounts-source');
    if (!select) return;
    const current = state.accounts.source;
    const options = [createElement('option', '', '全部批次')];
    options[0].value = 'all';
    sources.forEach((source) => {
      const option = createElement('option', '', source);
      option.value = source;
      options.push(option);
    });
    select.replaceChildren(...options);
    state.accounts.source = sources.includes(current) ? current : 'all';
    select.value = state.accounts.source;
  }

  function renderAccountRows(items = []) {
    const body = document.getElementById('accounts-table-body');
    const table = document.getElementById('accounts-table-wrap');
    const empty = document.getElementById('accounts-empty');
    if (!body || !table || !empty) return;
    body.replaceChildren();
    items.forEach((account) => {
      const row = document.createElement('tr');
      const email = document.createElement('td');
      email.className = 'account-email';
      email.textContent = account.email || '—';
      const source = document.createElement('td');
      source.className = 'table-muted';
      source.textContent = account.source || '—';
      const statusCell = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = 'status-badge';
      badge.dataset.status = account.status === 'ready' ? 'ready' : 'pending';
      badge.textContent = account.status === 'ready' ? 'CPA 就绪' : '待转换';
      statusCell.append(badge);
      const modified = document.createElement('td');
      modified.className = 'table-muted';
      modified.textContent = account.source_mtime || '—';
      row.append(email, source, statusCell, modified);
      body.append(row);
    });
    table.hidden = items.length === 0;
    empty.hidden = items.length !== 0;
  }

  function renderAccountFiles(files = []) {
    const body = document.getElementById('account-files-body');
    const empty = document.getElementById('account-files-empty');
    const selectAll = document.getElementById('account-files-select-all');
    if (!body || !empty || !selectAll) return;
    const availableNames = new Set(files.map((file) => file.name));
    state.accounts.selectedFiles.forEach((name) => {
      if (!availableNames.has(name)) state.accounts.selectedFiles.delete(name);
    });
    body.replaceChildren();
    files.forEach((file) => {
      const row = document.createElement('tr');
      const checkboxCell = document.createElement('td');
      checkboxCell.className = 'checkbox-cell';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = state.accounts.selectedFiles.has(file.name);
      checkbox.setAttribute('aria-label', `选择 ${file.name}`);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) state.accounts.selectedFiles.add(file.name);
        else state.accounts.selectedFiles.delete(file.name);
        renderAccountFiles(state.accounts.files);
      });
      checkboxCell.append(checkbox);

      const name = document.createElement('td');
      name.className = 'file-name';
      name.textContent = file.name;
      name.title = file.name;
      const count = document.createElement('td');
      count.className = 'table-muted';
      count.textContent = String(file.count || 0);
      const modified = document.createElement('td');
      modified.className = 'table-muted';
      modified.textContent = file.mtime || '—';
      const actions = document.createElement('td');
      actions.className = 'file-actions';
      const encodedName = encodeURIComponent(file.name);
      const preview = document.createElement('a');
      preview.href = `/preview/${encodedName}`;
      preview.target = '_blank';
      preview.rel = 'noopener';
      preview.textContent = '预览';
      const download = document.createElement('a');
      download.href = `/download/${encodedName}`;
      download.textContent = '下载';
      actions.append(preview, download);
      row.append(checkboxCell, name, count, modified, actions);
      body.append(row);
    });
    empty.hidden = files.length !== 0;
    selectAll.checked = files.length > 0 && state.accounts.selectedFiles.size === files.length;
    selectAll.indeterminate = state.accounts.selectedFiles.size > 0
      && state.accounts.selectedFiles.size < files.length;
    syncAccountControls();
  }

  async function loadAccounts() {
    const accounts = state.accounts;
    accounts.controller?.abort();
    accounts.controller = new AbortController();
    const requestGeneration = ++accounts.requestGeneration;
    accounts.loading = true;
    setBusy('accounts', true);
    setAccountsError();
    const params = new URLSearchParams({
      page: String(accounts.page),
      page_size: String(accounts.pageSize),
      q: accounts.q,
      source: accounts.source,
      status: accounts.status,
      sort: accounts.sort,
    });
    try {
      const payload = await requestJson(`/api/v2/accounts?${params.toString()}`, {
        signal: accounts.controller.signal,
      });
      if (requestGeneration !== accounts.requestGeneration) return;
      const pagination = payload.pagination || {};
      const totalPages = Number(pagination.total_pages || 0);
      if (totalPages > 0 && accounts.page > totalPages) {
        accounts.page = totalPages;
        await loadAccounts();
        return;
      }
      accounts.loaded = true;
      accounts.totalPages = totalPages;
      accounts.files = Array.isArray(payload.files) ? payload.files : [];
      updateAccountSources(payload.filters?.sources || []);
      renderAccountRows(Array.isArray(payload.items) ? payload.items : []);
      renderAccountFiles(accounts.files);
      setText('metric-accounts', Number(payload.summary?.total_accounts || 0));
      setText('accounts-result-count', `${Number(pagination.total || 0)} 个账号`);
      setText('accounts-page-label', totalPages
        ? `第 ${accounts.page} / ${totalPages} 页`
        : '第 0 / 0 页');
    } catch (error) {
      if (error?.name === 'AbortError') return;
      if (requestGeneration !== accounts.requestGeneration) return;
      accounts.loaded = false;
      setAccountsError(safeErrorMessage(error));
    } finally {
      if (requestGeneration === accounts.requestGeneration) {
        accounts.loading = false;
        setBusy('accounts', false);
      }
    }
  }

  function ensureAccountsLoaded() {
    if (!state.accounts.loaded && !state.accounts.loading) {
      loadAccounts().catch(() => {});
    }
  }

  function resetAccountPageAndLoad() {
    state.accounts.page = 1;
    loadAccounts().catch(() => {});
  }

  async function deleteSelectedAccountFiles() {
    const names = [...state.accounts.selectedFiles];
    if (!names.length) return;
    const accepted = await confirmAction({
      title: `删除 ${names.length} 个账号批次？`,
      message: '所选 TXT 文件将被删除；不再属于当前账号的 CPA 会移动到 archive。',
      acceptLabel: '删除所选',
    });
    if (!accepted) return;
    state.accounts.loading = true;
    setBusy('accounts', true);
    try {
      const payload = await requestJson('/api/accounts/delete', {
        method: 'POST',
        body: { files: names },
      });
      state.accounts.selectedFiles.clear();
      state.accounts.loaded = false;
      showToast(payload.message || '账号批次已删除');
      await loadAccounts();
      await Promise.allSettled([loadJobStatus({ silent: true }), loadCredentialSummary()]);
    } catch (error) {
      setAccountsError(safeErrorMessage(error));
    } finally {
      state.accounts.loading = false;
      setBusy('accounts', false);
    }
  }

  async function importCredentials(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = document.getElementById('credential-import-file')?.files?.[0];
    if (!file || !form.reportValidity()) return;
    const accepted = await confirmAction({
      title: '替换当前凭据工作区？',
      message: `将导入 ${file.name}，当前账号和 CPA 会先归档，再激活新批次。`,
      acceptLabel: '确认导入',
    });
    if (!accepted) return;
    const data = new FormData(form);
    state.accounts.loading = true;
    setBusy('accounts', true);
    try {
      const payload = await requestJson('/api/credentials/import', {
        method: 'POST',
        body: data,
      });
      showToast(`已导入 ${payload.parsed || 0} 个账号，CPA 入队 ${payload.queued || 0}`);
      form.reset();
      state.accounts.selectedFiles.clear();
      state.accounts.loaded = false;
      await Promise.allSettled([
        loadAccounts(),
        loadJobStatus({ silent: true }),
        loadCredentialSummary(),
      ]);
    } catch (error) {
      setAccountsError(safeErrorMessage(error));
    } finally {
      state.accounts.loading = false;
      setBusy('accounts', false);
    }
  }

  function renderJob(payload) {
    const job = payload?.job || {};
    const cpa = payload?.cpa || {};
    state.job = job;
    const running = Boolean(job.running);
    const count = Number(job.count || 0);
    const current = Math.min(Number(job.current_round || 0), count || Number(job.current_round || 0));
    const success = Number(job.success || 0);
    const fail = Number(job.fail || 0);
    const percent = count > 0 ? Math.min(100, Math.round((current / count) * 100)) : 0;

    const taskChip = document.getElementById('global-task-status');
    if (taskChip) taskChip.dataset.state = running ? 'running' : 'idle';
    setText('global-task-label', running ? '任务运行中' : '任务空闲');
    setText('metric-job-state', statusLabel(job.status));
    setText('metric-outcomes', `${success} / ${fail}`);
    setText('metric-concurrency', job.active_workers || 0);
    setText('metric-cpa', cpa.files || state.credentials?.stats?.cpa_files || 0);
    setText('overview-progress-label', running ? '注册进行中' : (count ? '最近任务' : '尚未运行任务'));
    setText('overview-progress-value', `${current} / ${count}`);
    setText('overview-progress-detail', running
      ? `并发 ${job.concurrency || 1} · 活跃 Worker ${job.active_workers || 0}`
      : (job.finished_at ? `完成于 ${job.finished_at}` : '进入注册页设置轮数、并发和浏览器模式。'));
    const progressBar = document.getElementById('overview-progress-bar');
    const progressFill = document.getElementById('overview-progress-fill');
    if (progressBar) progressBar.setAttribute('aria-valuenow', String(percent));
    if (progressFill) progressFill.style.width = `${percent}%`;

    const errorCard = document.getElementById('overview-last-error');
    const hasError = Boolean(job.last_error);
    if (errorCard) errorCard.dataset.hasError = String(hasError);
    setText('overview-last-error-text', hasError ? job.last_error : '任务错误会在这里显示脱敏摘要。');
    errorCard?.querySelector('strong')?.replaceChildren(
      document.createTextNode(hasError ? '需要处理' : '没有待处理异常'),
    );

    setText('registration-status', statusLabel(job.status));
    setText('registration-round', `${current} / ${count}`);
    setText('registration-success', success);
    setText('registration-fail', fail);
    setText('registration-workers', job.active_workers || 0);
    setText('registration-started', job.started_at || '—');
    setText('registration-message', running
      ? `任务运行中，目标 ${count} 轮，并发 ${job.concurrency || 1}。`
      : (job.finished_at ? `任务已结束：成功 ${success}，失败 ${fail}。` : '等待启动注册任务。'));
    renderWorkers(Array.isArray(job.workers) ? job.workers : []);
    syncRegistrationControls();
  }

  async function loadJobStatus({ silent = false } = {}) {
    try {
      const payload = await requestJson('/api/job/status');
      renderJob(payload);
      if (!silent) setInlineError('section-register-error');
      return payload;
    } catch (error) {
      if (!silent) setInlineError('section-register-error', safeErrorMessage(error));
      throw error;
    }
  }

  async function loadBrowserConfig() {
    const payload = await requestJson('/api/config/browser');
    state.browser = payload;
    const engine = document.getElementById('browser-engine');
    const mode = document.getElementById('browser-window-mode');
    if (engine) engine.value = payload.browser_engine || 'chromium';
    if (mode) mode.value = payload.browser_window_mode || 'hidden';
    syncRegistrationControls();
    return payload;
  }

  async function loadCredentialSummary() {
    const payload = await requestJson('/api/config/credentials');
    state.credentials = payload;
    if (!state.job) setText('metric-cpa', payload.stats?.cpa_files || 0);
    return payload;
  }

  async function loadEmailSummary() {
    const payload = await requestJson('/api/v2/config/email');
    state.email = payload.email;
    return payload;
  }

  function registrationPayload() {
    return {
      count: Number(document.getElementById('register-count')?.value || 1),
      concurrency: Number(document.getElementById('register-concurrency')?.value || 1),
      browser_engine: document.getElementById('browser-engine')?.value || 'chromium',
      browser_window_mode: document.getElementById('browser-window-mode')?.value || 'hidden',
    };
  }

  async function startRegistration(event) {
    event.preventDefault();
    const form = document.getElementById('registration-form');
    if (!form?.reportValidity()) return;
    setBusy('registration', true);
    setInlineError('section-register-error');
    try {
      const payload = await requestJson('/api/job/start', {
        method: 'POST',
        body: registrationPayload(),
      });
      showToast(payload.message || '注册任务已启动');
      await loadJobStatus({ silent: true });
    } catch (error) {
      setInlineError('section-register-error', safeErrorMessage(error));
    } finally {
      setBusy('registration', false);
    }
  }

  async function stopRegistration() {
    const accepted = await confirmAction({
      title: '停止当前注册任务？',
      message: '正在运行的 Worker 和浏览器将被终止，已成功保存的账号不会删除。',
      acceptLabel: '停止任务',
    });
    if (!accepted) return;
    setBusy('registration', true);
    try {
      const payload = await requestJson('/api/job/stop', { method: 'POST' });
      showToast(payload.message || '已发送停止请求');
      await loadJobStatus({ silent: true });
    } catch (error) {
      setInlineError('section-register-error', safeErrorMessage(error));
    } finally {
      setBusy('registration', false);
    }
  }

  async function saveBrowserSettings() {
    setBusy('registration', true);
    setInlineError('section-register-error');
    try {
      const payload = registrationPayload();
      const result = await requestJson('/api/config/browser', {
        method: 'POST',
        body: {
          browser_engine: payload.browser_engine,
          browser_window_mode: payload.browser_window_mode,
        },
      });
      state.browser = result;
      showToast(result.message || '浏览器设置已保存');
    } catch (error) {
      setInlineError('section-register-error', safeErrorMessage(error));
    } finally {
      setBusy('registration', false);
    }
  }

  async function initialiseData() {
    const results = await Promise.allSettled([
      loadJobStatus(),
      loadBrowserConfig(),
      loadCredentialSummary(),
      loadEmailSummary(),
    ]);
    const failures = results.filter((result) => result.status === 'rejected');
    if (failures.length) showToast(`有 ${failures.length} 项状态暂时无法加载`, 'error');
    if (!state.pollTimer) {
      state.pollTimer = window.setInterval(() => {
        loadJobStatus({ silent: true }).catch(() => {});
      }, 2000);
    }
  }

  function bindEvents() {
    document.getElementById('theme-toggle')?.addEventListener('change', (event) => {
      applyTheme(event.target.value);
    });
    document.querySelectorAll('[data-section-link]').forEach((link) => {
      link.addEventListener('click', () => showSection(link.dataset.sectionLink));
    });
    document.getElementById('registration-form')?.addEventListener('submit', startRegistration);
    document.getElementById('stop-registration')?.addEventListener('click', stopRegistration);
    document.getElementById('save-browser-settings')?.addEventListener('click', saveBrowserSettings);
    document.getElementById('browser-engine')?.addEventListener('change', syncRegistrationControls);
    document.getElementById('accounts-search')?.addEventListener('input', (event) => {
      window.clearTimeout(state.accounts.searchTimer);
      state.accounts.searchTimer = window.setTimeout(() => {
        state.accounts.q = event.target.value.trim();
        resetAccountPageAndLoad();
      }, 250);
    });
    document.getElementById('accounts-source')?.addEventListener('change', (event) => {
      state.accounts.source = event.target.value || 'all';
      resetAccountPageAndLoad();
    });
    document.getElementById('accounts-status')?.addEventListener('change', (event) => {
      state.accounts.status = event.target.value || 'all';
      resetAccountPageAndLoad();
    });
    document.getElementById('accounts-sort')?.addEventListener('change', (event) => {
      state.accounts.sort = event.target.value || 'newest';
      resetAccountPageAndLoad();
    });
    document.getElementById('accounts-page-size')?.addEventListener('change', (event) => {
      const size = Number(event.target.value);
      state.accounts.pageSize = [25, 50, 100].includes(size) ? size : 25;
      savePreference(ACCOUNT_PAGE_SIZE_KEY, String(state.accounts.pageSize));
      resetAccountPageAndLoad();
    });
    document.getElementById('accounts-prev')?.addEventListener('click', () => {
      if (state.accounts.page <= 1) return;
      state.accounts.page -= 1;
      loadAccounts().catch(() => {});
    });
    document.getElementById('accounts-next')?.addEventListener('click', () => {
      if (state.accounts.page >= state.accounts.totalPages) return;
      state.accounts.page += 1;
      loadAccounts().catch(() => {});
    });
    document.getElementById('accounts-retry')?.addEventListener('click', () => {
      loadAccounts().catch(() => {});
    });
    document.getElementById('account-files-select-all')?.addEventListener('change', (event) => {
      state.accounts.selectedFiles.clear();
      if (event.target.checked) {
        state.accounts.files.forEach((file) => state.accounts.selectedFiles.add(file.name));
      }
      renderAccountFiles(state.accounts.files);
    });
    document.getElementById('account-files-delete')?.addEventListener('click', deleteSelectedAccountFiles);
    document.getElementById('credential-import-form')?.addEventListener('submit', importCredentials);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) loadJobStatus({ silent: true }).catch(() => {});
    });
  }

  async function initialise() {
    const preference = readPreference(THEME_KEY, 'system');
    const savedPageSize = Number(readPreference(ACCOUNT_PAGE_SIZE_KEY, '25'));
    state.accounts.pageSize = [25, 50, 100].includes(savedPageSize) ? savedPageSize : 25;
    const pageSize = document.getElementById('accounts-page-size');
    if (pageSize) pageSize.value = String(state.accounts.pageSize);
    applyTheme(preference, false);
    showSection(requestedSection());
    bindEvents();
    await initialiseData();
  }

  window.addEventListener('hashchange', () => showSection(requestedSection()));
  systemTheme.addEventListener?.('change', () => {
    if (document.documentElement.dataset.themePreference === 'system') {
      applyTheme('system', false);
    }
  });
  document.addEventListener('DOMContentLoaded', () => initialise().catch((error) => {
    showToast(safeErrorMessage(error), 'error');
  }), { once: true });
})();
