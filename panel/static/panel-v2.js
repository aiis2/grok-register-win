(() => {
  'use strict';

  const THEME_KEY = 'panel-v2-theme';
  const SECTION_KEY = 'panel-v2-section';
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
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value ?? '');
  }

  function initialise() {
    const preference = readPreference(THEME_KEY, 'system');
    applyTheme(preference, false);
    showSection(requestedSection());
    setText('global-task-label', '任务空闲');

    document.getElementById('theme-toggle')?.addEventListener('change', (event) => {
      applyTheme(event.target.value);
    });
    document.querySelectorAll('[data-section-link]').forEach((link) => {
      link.addEventListener('click', () => showSection(link.dataset.sectionLink));
    });
  }

  window.addEventListener('hashchange', () => showSection(requestedSection()));
  systemTheme.addEventListener?.('change', () => {
    if (document.documentElement.dataset.themePreference === 'system') {
      applyTheme('system', false);
    }
  });
  document.addEventListener('DOMContentLoaded', initialise, { once: true });
})();
