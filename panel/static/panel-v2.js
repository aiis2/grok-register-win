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
  const EMAIL_VALUE_FIELDS = [
    'cfworker_api_url',
    'cfworker_domain',
    'cfworker_subdomain',
    'cloudflare_api_base',
    'cloudflare_domain',
    'moemail_api_url',
    'gptmail_base_url',
    'gptmail_domain',
    'duckmail_api_url',
    'duckmail_provider_url',
    'duckmail_domain',
    'maliapi_base_url',
    'maliapi_domain',
    'luckmail_base_url',
    'luckmail_project_code',
    'luckmail_domain',
    'skymail_api_base',
    'skymail_domain',
    'cloudmail_api_base',
    'cloudmail_admin_email',
    'cloudmail_domain',
    'freemail_api_url',
    'freemail_username',
    'freemail_domain',
    'mail_test_sender_mode',
    'mail_test_timeout_sec',
    'mail_test_smtp_host',
    'mail_test_smtp_port',
    'mail_test_smtp_security',
    'mail_test_smtp_username',
    'mail_test_smtp_from',
    'mail_test_direct_mx_enabled',
    'opentrashmail_api_url',
    'opentrashmail_domain',
    'laoudo_email',
    'laoudo_account_id',
  ];
  const EMAIL_SECRET_FIELDS = [
    'cfworker_admin_token',
    'cfworker_custom_auth',
    'cloudflare_admin_password',
    'cloudflare_site_password',
    'moemail_api_key',
    'gptmail_api_key',
    'duckmail_bearer',
    'duckmail_api_key',
    'maliapi_api_key',
    'luckmail_api_key',
    'skymail_token',
    'cloudmail_admin_password',
    'freemail_admin_token',
    'freemail_password',
    'opentrashmail_password',
    'laoudo_auth',
    'mail_test_smtp_password',
  ];
  const EMAIL_FIELD_IDS = {
    mail_test_sender_mode: 'mail-test-sender-mode',
    mail_test_timeout_sec: 'mail-test-timeout-sec',
    mail_test_smtp_host: 'mail-test-smtp-host',
    mail_test_smtp_port: 'mail-test-smtp-port',
    mail_test_smtp_security: 'mail-test-smtp-security',
    mail_test_smtp_username: 'mail-test-smtp-username',
    mail_test_smtp_password: 'mail-test-smtp-password',
    mail_test_smtp_from: 'mail-test-smtp-from',
    mail_test_direct_mx_enabled: 'mail-test-direct-mx-enabled',
  };
  const EMAIL_BOOLEAN_FIELDS = new Set(['mail_test_direct_mx_enabled']);
  const EMAIL_NUMBER_FIELDS = new Set(['mail_test_timeout_sec', 'mail_test_smtp_port']);
  const EMAIL_RECEIVE_STAGES = [
    ['checking', '检查配置'],
    ['creating', '创建邮箱'],
    ['snapshotting', '收件快照'],
    ['sending', '发送测试信'],
    ['waiting', '等待收件'],
    ['verifying', '核对验证码'],
    ['cleaning', '清理邮箱'],
    ['succeeded', '验证成功'],
  ];
  const EMAIL_RECEIVE_TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
  const EMAIL_RECEIVE_SESSION_KEY = 'panel-v2-email-receive-test-id';
  const restoredEmailTestId = readSessionValue(EMAIL_RECEIVE_SESSION_KEY);
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const state = {
    job: null,
    browser: null,
    credentials: null,
    cpa: null,
    email: null,
    emailReceive: {
      testId: restoredEmailTestId,
      pollTimer: null,
      running: Boolean(restoredEmailTestId),
      capabilityReady: false,
      cancelRequested: false,
    },
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

  function readSessionValue(key) {
    try {
      return window.sessionStorage.getItem(key) || '';
    } catch (_) {
      return '';
    }
  }

  function saveSessionValue(key, value) {
    try {
      if (value) window.sessionStorage.setItem(key, value);
      else window.sessionStorage.removeItem(key);
    } catch (_) {}
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
    if (group === 'mail') syncMailControls();
    if (group === 'credentials') syncCredentialControls();
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
    const receiveTestRunning = state.emailReceive.running;
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
    if (start) start.disabled = running || busy || receiveTestRunning;
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
    renderCpaStatus(cpa);
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
    syncMailControls();
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

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value || 0));
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = bytes;
    let unit = 'B';
    for (const next of units) {
      size /= 1024;
      unit = next;
      if (size < 1024) break;
    }
    const digits = size >= 10 ? 1 : 2;
    return `${Number(size.toFixed(digits))} ${unit}`;
  }

  function renderCredentialSummary(payload = {}) {
    state.credentials = payload;
    const stats = payload.stats || {};
    setText('credentials-sso-files', Number(stats.sso_files || 0));
    setText('credentials-mail-files', Number(stats.mail_files || 0));
    setText('credentials-cpa-files', Number(stats.cpa_files || 0));
    setText('credentials-total-files', Number(stats.total_files || 0));
    setText('credentials-total-bytes', formatBytes(stats.total_bytes));
    setText('credentials-resolved-path', payload.resolved_path || '—');
    setText('credentials-legacy-files', `历史位置待迁移：${Number(payload.legacy_files || 0)} 个文件`);
    const pathInput = document.getElementById('credentials-dir');
    if (pathInput && document.activeElement !== pathInput) {
      pathInput.value = payload.configured || 'data/credentials';
    }
    const writable = document.getElementById('credentials-writable');
    if (writable) {
      writable.textContent = payload.writable ? '可写' : '不可写';
      writable.classList.toggle('status-success', Boolean(payload.writable));
      writable.classList.toggle('status-danger', !payload.writable);
    }
    if (!state.job) setText('metric-cpa', stats.cpa_files || 0);
    syncCredentialControls();
  }

  async function loadCredentialSummary() {
    try {
      const payload = await requestJson('/api/config/credentials');
      renderCredentialSummary(payload);
      setInlineError('section-credentials-error');
      return payload;
    } catch (error) {
      setInlineError('section-credentials-error', safeErrorMessage(error));
      throw error;
    }
  }

  function renderCpaStatus(cpa = {}) {
    state.cpa = cpa;
    const status = document.getElementById('cpa-status');
    if (status) {
      let label = '就绪';
      let tone = 'status-success';
      if (cpa.core_ok === false) {
        label = '转换核心不可用';
        tone = 'status-danger';
      } else if (cpa.running || Number(cpa.pending || 0) > 0) {
        label = '转换中';
        tone = '';
      } else if (Number(cpa.fail || 0) > 0) {
        label = '存在失败';
        tone = 'status-danger';
      }
      status.textContent = label;
      status.classList.toggle('status-success', tone === 'status-success');
      status.classList.toggle('status-danger', tone === 'status-danger');
    }
    setText('cpa-done', Number(cpa.done ?? cpa.files ?? 0));
    setText('cpa-pending', Number(cpa.pending || 0));
    setText('cpa-fail', Number(cpa.fail || 0));
    setText('cpa-last-email', cpa.last_ok_email || '—');
    syncCredentialControls();
  }

  async function loadCpaStatus() {
    const payload = await requestJson('/api/cpa/status');
    renderCpaStatus(payload.cpa || {});
    return payload;
  }

  function syncCredentialControls() {
    const busy = state.busy.has('credentials');
    const blocked = Boolean(state.job?.running)
      || Boolean(state.cpa?.running)
      || Number(state.cpa?.pending || 0) > 0;
    const pathInput = document.getElementById('credentials-dir');
    const save = document.getElementById('credentials-save');
    const migrate = document.getElementById('credentials-migrate');
    const backfill = document.getElementById('cpa-backfill');
    const limit = document.getElementById('cpa-backfill-limit');
    if (pathInput) pathInput.disabled = busy || blocked;
    if (save) save.disabled = busy || blocked;
    if (migrate) migrate.disabled = busy || blocked;
    if (limit) limit.disabled = busy || Boolean(state.job?.running);
    if (backfill) backfill.disabled = busy || Boolean(state.job?.running) || state.cpa?.core_ok === false;
  }

  function requestedCredentialPath() {
    return String(document.getElementById('credentials-dir')?.value || '').trim();
  }

  async function saveCredentialDirectory() {
    const credentialsDir = requestedCredentialPath();
    if (!credentialsDir) {
      setInlineError('section-credentials-error', '请输入凭据根目录');
      document.getElementById('credentials-dir')?.focus();
      return;
    }
    setBusy('credentials', true);
    setInlineError('section-credentials-error');
    try {
      const payload = await requestJson('/api/config/credentials', {
        method: 'POST',
        body: { credentials_dir: credentialsDir },
      });
      renderCredentialSummary(payload);
      showToast('凭据目录已保存');
    } catch (error) {
      setInlineError('section-credentials-error', safeErrorMessage(error));
    } finally {
      setBusy('credentials', false);
    }
  }

  async function migrateCredentialDirectory() {
    const credentialsDir = requestedCredentialPath();
    if (!credentialsDir) {
      setInlineError('section-credentials-error', '请输入目标凭据根目录');
      document.getElementById('credentials-dir')?.focus();
      return;
    }
    const accepted = await confirmAction({
      title: '迁移全部账号凭据并切换目录？',
      message: `将当前 SSO、邮箱和 CPA 凭据一起迁移到 ${credentialsDir}。文件会先复制并校验，再切换配置。`,
      acceptLabel: '迁移并切换',
    });
    if (!accepted) return;
    setBusy('credentials', true);
    setInlineError('section-credentials-error');
    try {
      const payload = await requestJson('/api/config/credentials/migrate', {
        method: 'POST',
        body: { credentials_dir: credentialsDir },
      });
      renderCredentialSummary(payload);
      const migration = payload.migration || {};
      showToast(`迁移完成：复制 ${Number(migration.copied || 0)}，移除来源 ${Number(migration.removed || 0)}`);
      state.accounts.loaded = false;
      await Promise.allSettled([
        loadCpaStatus(),
        loadJobStatus({ silent: true }),
        loadAccounts(),
      ]);
    } catch (error) {
      setInlineError('section-credentials-error', safeErrorMessage(error));
    } finally {
      setBusy('credentials', false);
    }
  }

  async function backfillCpa() {
    const limitInput = document.getElementById('cpa-backfill-limit');
    if (!limitInput?.reportValidity()) return;
    const limit = Math.max(1, Math.min(1000, Number(limitInput.value || 200)));
    setBusy('credentials', true);
    setInlineError('section-credentials-error');
    try {
      const payload = await requestJson('/api/cpa/backfill', {
        method: 'POST',
        body: { limit },
      });
      showToast(payload.message || `已入队 ${Number(payload.queued || 0)} 个待转换账号`);
      await Promise.allSettled([loadCpaStatus(), loadJobStatus({ silent: true })]);
      window.setTimeout(() => loadCpaStatus().catch(() => {}), 1200);
    } catch (error) {
      setInlineError('section-credentials-error', safeErrorMessage(error));
    } finally {
      setBusy('credentials', false);
    }
  }

  function emailElement(field) {
    return document.getElementById(EMAIL_FIELD_IDS[field] || field);
  }

  function showSelectedMailProvider() {
    const provider = document.getElementById('email-provider')?.value || 'cfworker';
    document.querySelectorAll('[data-mail-provider]').forEach((panel) => {
      panel.hidden = panel.dataset.mailProvider !== provider;
    });
    const connection = document.getElementById('email-connection-test');
    if (connection) connection.hidden = provider !== 'cloudflare_temp_email';
    syncMailControls();
  }

  function renderEmailConfig(email = {}) {
    state.email = email;
    const providerSelect = document.getElementById('email-provider');
    const choices = Array.isArray(email.choices) ? email.choices : [];
    if (providerSelect) {
      const options = choices.map((choice) => {
        const option = createElement('option', '', choice.label || choice.id || '未知邮箱源');
        option.value = choice.id || '';
        return option;
      });
      providerSelect.replaceChildren(...options);
      providerSelect.value = email.provider || choices[0]?.id || 'cfworker';
    }
    const values = email.values || {};
    const failover = document.getElementById('email-failover');
    if (failover) failover.checked = Boolean(values.email_failover);
    EMAIL_VALUE_FIELDS.forEach((field) => {
      const input = emailElement(field);
      if (!input) return;
      if (EMAIL_BOOLEAN_FIELDS.has(field)) input.checked = Boolean(values[field]);
      else input.value = values[field] ?? '';
    });
    const useEnvironment = document.getElementById('freemail_use_environment');
    if (useEnvironment) useEnvironment.checked = email.freemail_auth_source === 'environment';
    const configured = email.configured || {};
    EMAIL_SECRET_FIELDS.forEach((field) => {
      const input = emailElement(field);
      if (!input) return;
      input.value = '';
      input.placeholder = configured[field] ? '已保存；留空不修改' : '未配置';
    });
    const environment = email.environment || {};
    const availableCount = [
      environment.freemail_url_available,
      environment.freemail_username_available,
      environment.freemail_password_available,
    ].filter(Boolean).length;
    const sourceLabels = {
      saved_token: '页面 Admin Token',
      saved_login: '页面账号密码',
      environment: 'Windows 环境变量',
      none: '未配置',
    };
    setText(
      'freemail-source-hint',
      `当前认证来源：${sourceLabels[email.freemail_auth_source] || '未知'}；环境变量就绪 ${availableCount}/3。环境变量内容不会返回页面。`,
    );
    setText('email-hint', email.hint || '邮箱配置已安全加载；密钥输入不会回显。');
    showSelectedMailProvider();
  }

  async function loadEmailSummary() {
    try {
      const payload = await requestJson('/api/v2/config/email');
      renderEmailConfig(payload.email || {});
      setInlineError('section-mail-error');
      return payload;
    } catch (error) {
      setInlineError('section-mail-error', safeErrorMessage(error));
      throw error;
    }
  }

  function buildEmailPayload() {
    const payload = {
      provider: document.getElementById('email-provider')?.value || 'cfworker',
      email_failover: Boolean(document.getElementById('email-failover')?.checked),
      freemail_use_environment: Boolean(document.getElementById('freemail_use_environment')?.checked),
    };
    EMAIL_VALUE_FIELDS.forEach((field) => {
      const input = emailElement(field);
      if (!input) return;
      if (EMAIL_BOOLEAN_FIELDS.has(field)) {
        payload[field] = Boolean(input.checked);
      } else if (EMAIL_NUMBER_FIELDS.has(field)) {
        payload[field] = Number(input.value);
      } else {
        payload[field] = String(input.value || '').trim();
      }
    });
    EMAIL_SECRET_FIELDS.forEach((field) => {
      const value = String(emailElement(field)?.value || '').trim();
      if (value) payload[field] = value;
    });
    return payload;
  }

  async function saveEmailConfig() {
    setBusy('mail', true);
    setInlineError('section-mail-error');
    try {
      const payload = await requestJson('/api/v2/config/email', {
        method: 'POST',
        body: buildEmailPayload(),
      });
      renderEmailConfig(payload.email || {});
      showToast(payload.message || '邮箱设置已保存');
    } catch (error) {
      setInlineError('section-mail-error', safeErrorMessage(error));
    } finally {
      setBusy('mail', false);
    }
  }

  async function testEmailConnection() {
    setBusy('mail', true);
    setInlineError('section-mail-error');
    try {
      const payload = await requestJson('/api/v2/config/email/test', {
        method: 'POST',
        body: buildEmailPayload(),
      });
      const message = payload.message || 'Cloudflare Temp Email 连接成功';
      setText('email-hint', message);
      showToast(message);
    } catch (error) {
      const message = safeErrorMessage(error);
      setInlineError('section-mail-error', message);
      setText('email-hint', `连接失败：${message}`);
    } finally {
      setBusy('mail', false);
    }
  }

  function syncMailControls(receiveState = null) {
    if (receiveState) {
      state.emailReceive.running = Boolean(receiveState.running);
      state.emailReceive.cancelRequested = Boolean(receiveState.cancel_requested);
    }
    const busy = state.busy.has('mail');
    const running = state.emailReceive.running;
    const registrationRunning = Boolean(state.job?.running);
    document.querySelectorAll('[data-mail-field], [data-mail-secret], #email-provider, #email-failover, #freemail_use_environment').forEach((control) => {
      control.disabled = busy || running;
    });
    const save = document.getElementById('email-save');
    const connection = document.getElementById('email-connection-test');
    const open = document.getElementById('email-receive-test-open');
    const start = document.getElementById('email-receive-start');
    const cancel = document.getElementById('email-receive-cancel');
    if (save) save.disabled = busy || running;
    if (connection) connection.disabled = busy || running;
    if (open) {
      open.disabled = busy || (registrationRunning && !running);
      open.textContent = running ? '查看测试进度' : '测试收件';
    }
    if (start) start.disabled = busy || running || registrationRunning || !state.emailReceive.capabilityReady;
    if (cancel) cancel.disabled = busy || !running || state.emailReceive.cancelRequested;
    syncRegistrationControls();
  }

  function setEmailReceiveMessage(message, tone = '') {
    const element = document.getElementById('email-receive-message');
    if (!element) return;
    element.textContent = String(message || '');
    if (tone) element.dataset.tone = tone;
    else delete element.dataset.tone;
  }

  function renderEmailReceiveTimeline(status = 'checking', errorStage = '') {
    const timeline = document.getElementById('email-receive-timeline');
    if (!timeline) return;
    const effective = EMAIL_RECEIVE_TERMINAL.has(status) && status !== 'succeeded'
      ? (errorStage || 'checking')
      : status;
    const activeIndex = Math.max(0, EMAIL_RECEIVE_STAGES.findIndex(([stage]) => stage === effective));
    const items = EMAIL_RECEIVE_STAGES.map(([stage, label], index) => {
      const item = createElement('li', '', label);
      item.dataset.stage = stage;
      if (status === 'succeeded' || index < activeIndex) item.dataset.state = 'done';
      if (status === 'succeeded' && stage === 'succeeded') item.dataset.state = 'current';
      if (index === activeIndex && status !== 'succeeded') {
        item.dataset.state = EMAIL_RECEIVE_TERMINAL.has(status) ? 'failed' : 'current';
        item.setAttribute('aria-current', 'step');
      }
      return item;
    });
    timeline.replaceChildren(...items);
  }

  function renderEmailReceiveTest(test = {}) {
    const status = String(test.status || 'checking');
    setText('email-receive-provider', test.provider || '—');
    setText('email-receive-sender', test.sender_mode || '选择中');
    setText('email-receive-address', test.email || '尚未创建');
    renderEmailReceiveTimeline(status, test.error_stage || '');
    const timings = [];
    if (test.total_sec !== null && test.total_sec !== undefined) {
      timings.push(`总耗时 ${Number(test.total_sec).toFixed(1)} 秒`);
    }
    if (test.receive_sec !== null && test.receive_sec !== undefined) {
      timings.push(`收件等待 ${Number(test.receive_sec).toFixed(1)} 秒`);
    }
    if (test.cleanup && test.cleanup !== 'not_needed') timings.push(`清理 ${test.cleanup}`);
    setText('email-receive-timing', timings.join(' · ') || '测试进行中');
    if (status === 'succeeded') {
      const warnings = Array.isArray(test.warnings) && test.warnings.length
        ? `\n警告：${test.warnings.join('；')}`
        : '';
      setEmailReceiveMessage(`收件验证成功。邮箱源、发件链路与验证码读取均可用。${warnings}`, 'success');
    } else if (status === 'failed') {
      setEmailReceiveMessage(`失败阶段 ${test.error_stage || status}：${test.error || '邮箱收件测试失败'}`, 'error');
    } else if (status === 'cancelled') {
      setEmailReceiveMessage(`测试已取消${test.error_stage ? `（阶段 ${test.error_stage}）` : ''}`, 'error');
    } else {
      const stage = EMAIL_RECEIVE_STAGES.find(([name]) => name === status);
      setEmailReceiveMessage(`${stage?.[1] || status}… 关闭窗口不会取消服务端测试。`);
    }
    state.emailReceive.capabilityReady = EMAIL_RECEIVE_TERMINAL.has(status)
      || state.emailReceive.capabilityReady;
    syncMailControls(test);
  }

  function stopEmailReceivePolling() {
    if (state.emailReceive.pollTimer) {
      window.clearTimeout(state.emailReceive.pollTimer);
      state.emailReceive.pollTimer = null;
    }
  }

  function closeEmailReceiveDialog() {
    stopEmailReceivePolling();
    const dialog = document.getElementById('email-receive-dialog');
    if (dialog?.open) dialog.close();
  }

  function scheduleEmailReceivePoll(delay = 1000) {
    stopEmailReceivePolling();
    const dialog = document.getElementById('email-receive-dialog');
    if (!dialog?.open || !state.emailReceive.testId) return;
    state.emailReceive.pollTimer = window.setTimeout(() => {
      pollEmailReceiveTest().catch(() => {});
    }, delay);
  }

  async function openEmailReceiveTest() {
    const dialog = document.getElementById('email-receive-dialog');
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    if (state.emailReceive.testId) {
      state.emailReceive.capabilityReady = true;
      await pollEmailReceiveTest();
      return;
    }
    state.emailReceive.capabilityReady = false;
    state.emailReceive.cancelRequested = false;
    renderEmailReceiveTimeline('checking');
    setText('email-receive-provider', document.getElementById('email-provider')?.value || '—');
    setText('email-receive-sender', '能力探测中');
    setText('email-receive-address', '尚未创建');
    setText('email-receive-timing', '尚未开始');
    setEmailReceiveMessage('正在主动检查当前配置支持的发件方式…');
    setBusy('mail', true);
    try {
      const payload = await requestJson('/api/config/email/test-capabilities', {
        method: 'POST',
        body: buildEmailPayload(),
      });
      const capabilities = Array.isArray(payload.capabilities) ? payload.capabilities : [];
      state.emailReceive.capabilityReady = capabilities.some((item) => item.available);
      setText('email-receive-provider', payload.provider || '—');
      setText('email-receive-sender', payload.selected_mode || '无可用策略');
      const modeLabels = { native: '原生 API', smtp: 'SMTP Relay', direct_mx: 'Direct MX' };
      const descriptions = capabilities.map((item) => {
        const label = modeLabels[item.mode] || item.mode || '未知方式';
        if (item.available) return `${label}：可用${item.reason ? `（${item.reason}）` : ''}`;
        return `${label}：${item.reason || '不可用'}`;
      });
      const action = state.emailReceive.capabilityReady
        ? '点击“发送验证码并测试”开始。'
        : '请先补全一种可用发件方式。';
      setEmailReceiveMessage([...descriptions, action].join('\n'), state.emailReceive.capabilityReady ? '' : 'error');
    } catch (error) {
      state.emailReceive.capabilityReady = false;
      setEmailReceiveMessage(`能力检查失败：${safeErrorMessage(error)}`, 'error');
    } finally {
      setBusy('mail', false);
    }
  }

  async function startEmailReceiveTest() {
    if (state.emailReceive.running) return;
    state.emailReceive.cancelRequested = false;
    setBusy('mail', true);
    setEmailReceiveMessage('正在创建邮箱收件测试…');
    try {
      const payload = await requestJson('/api/config/email/receive-test', {
        method: 'POST',
        body: buildEmailPayload(),
      });
      const test = payload.test || {};
      state.emailReceive.testId = test.test_id || '';
      state.emailReceive.running = Boolean(test.running ?? state.emailReceive.testId);
      saveSessionValue(EMAIL_RECEIVE_SESSION_KEY, state.emailReceive.testId);
      renderEmailReceiveTest(test);
      scheduleEmailReceivePoll(500);
    } catch (error) {
      state.emailReceive.running = false;
      setEmailReceiveMessage(`启动失败：${safeErrorMessage(error)}`, 'error');
    } finally {
      setBusy('mail', false);
    }
  }

  async function pollEmailReceiveTest() {
    stopEmailReceivePolling();
    const testId = state.emailReceive.testId;
    if (!testId) return;
    try {
      const payload = await requestJson(`/api/config/email/receive-test/${encodeURIComponent(testId)}`);
      const test = payload.test || {};
      renderEmailReceiveTest(test);
      const terminal = EMAIL_RECEIVE_TERMINAL.has(String(test.status || ''));
      if (terminal) {
        state.emailReceive.running = false;
        state.emailReceive.testId = '';
        saveSessionValue(EMAIL_RECEIVE_SESSION_KEY, '');
        syncMailControls(test);
        return;
      }
      scheduleEmailReceivePoll();
    } catch (error) {
      state.emailReceive.running = false;
      state.emailReceive.testId = '';
      saveSessionValue(EMAIL_RECEIVE_SESSION_KEY, '');
      setEmailReceiveMessage(`读取测试状态失败：${safeErrorMessage(error)}`, 'error');
      syncMailControls();
    }
  }

  async function cancelEmailReceiveTest() {
    const testId = state.emailReceive.testId;
    if (!testId || state.emailReceive.cancelRequested) return;
    state.emailReceive.cancelRequested = true;
    syncMailControls();
    try {
      const payload = await requestJson(`/api/config/email/receive-test/${encodeURIComponent(testId)}/cancel`, {
        method: 'POST',
      });
      renderEmailReceiveTest(payload.test || {});
      setEmailReceiveMessage('已请求取消，正在安全清理测试邮箱…');
      scheduleEmailReceivePoll(300);
    } catch (error) {
      state.emailReceive.cancelRequested = false;
      setEmailReceiveMessage(`取消失败：${safeErrorMessage(error)}`, 'error');
      syncMailControls();
    }
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
      loadCpaStatus(),
    ]);
    const failures = results.filter((result) => result.status === 'rejected');
    if (failures.length) showToast(`有 ${failures.length} 项状态暂时无法加载`, 'error');
    if (!state.pollTimer) {
      state.pollTimer = window.setInterval(() => {
        loadJobStatus({ silent: true }).catch(() => {});
      }, 2000);
    }
    if (state.emailReceive.testId) {
      pollEmailReceiveTest().catch(() => {});
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
    document.getElementById('email-provider')?.addEventListener('change', showSelectedMailProvider);
    document.getElementById('email-save')?.addEventListener('click', saveEmailConfig);
    document.getElementById('email-connection-test')?.addEventListener('click', testEmailConnection);
    document.getElementById('email-receive-test-open')?.addEventListener('click', openEmailReceiveTest);
    document.getElementById('email-receive-start')?.addEventListener('click', startEmailReceiveTest);
    document.getElementById('email-receive-cancel')?.addEventListener('click', cancelEmailReceiveTest);
    document.getElementById('email-receive-close')?.addEventListener('click', closeEmailReceiveDialog);
    const emailReceiveDialog = document.getElementById('email-receive-dialog');
    emailReceiveDialog?.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeEmailReceiveDialog();
    });
    emailReceiveDialog?.addEventListener('close', stopEmailReceivePolling);
    document.getElementById('credentials-save')?.addEventListener('click', saveCredentialDirectory);
    document.getElementById('credentials-migrate')?.addEventListener('click', migrateCredentialDirectory);
    document.getElementById('cpa-backfill')?.addEventListener('click', backfillCpa);
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
