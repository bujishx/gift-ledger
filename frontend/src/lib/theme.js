import { ref } from 'vue';

// A display preference belongs to this browser/device, never to a session or ledger.
export const THEME_STORAGE_KEY = 'gift-ledger-display-theme';
export const THEME_MODES = ['system', 'light', 'dark'];
export const themeMode = ref('system');
export const effectiveTheme = ref('light');
let controller;

export function createThemeController({ window: win, document: doc, onApply = () => {} }) {
  const media = win.matchMedia('(prefers-color-scheme: dark)');
  let mode = 'system';
  const valid = value => THEME_MODES.includes(value) ? value : 'system';
  const read = () => { try { return valid(win.localStorage.getItem(THEME_STORAGE_KEY)); } catch { return 'system'; } };
  function apply() {
    const theme = mode === 'system' ? (media.matches ? 'dark' : 'light') : mode;
    doc.documentElement.dataset.theme = theme;
    doc.documentElement.dataset.themeMode = mode;
    doc.documentElement.style.colorScheme = theme;
    onApply({ mode, theme });
    return { mode, theme };
  }
  const systemChanged = () => { if (mode === 'system') apply(); };
  const storageChanged = event => {
    if (event.key !== THEME_STORAGE_KEY && event.key !== null) return;
    // A cleared preference returns to the default, even when storage is unavailable.
    mode = event.key === null ? read() : valid(event.newValue);
    apply();
  };
  mode = read();
  if (media.addEventListener) media.addEventListener('change', systemChanged);
  else media.addListener(systemChanged);
  win.addEventListener('storage', storageChanged);
  apply();
  function setMode(value) {
    if (!THEME_MODES.includes(value)) return false;
    mode = value;
    try { win.localStorage.setItem(THEME_STORAGE_KEY, mode); } catch { /* Preference still works for this page. */ }
    apply();
    return true;
  }
  return {
    setMode,
    toggle() {
      const system = media.matches ? 'dark' : 'light';
      const current = mode === 'system' ? system : mode;
      const next = current === 'dark' ? 'light' : 'dark';
      setMode(next === system ? 'system' : next);
    },
    destroy() {
      if (media.removeEventListener) media.removeEventListener('change', systemChanged);
      else media.removeListener(systemChanged);
      win.removeEventListener('storage', storageChanged);
    },
  };
}

export function initializeTheme() {
  if (controller || typeof window === 'undefined') return controller;
  controller = createThemeController({ window, document, onApply: state => {
    themeMode.value = state.mode;
    effectiveTheme.value = state.theme;
  } });
  return controller;
}

export function setThemeMode(mode) {
  initializeTheme()?.setMode(mode);
}

export function useTheme() {
  initializeTheme();
  return { themeMode, effectiveTheme, setThemeMode, toggleTheme: () => controller?.toggle() };
}
