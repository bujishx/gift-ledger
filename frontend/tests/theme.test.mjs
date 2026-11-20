import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { createThemeController, THEME_STORAGE_KEY } from '../src/lib/theme.js';

function environment({ dark = false, saved = null, unavailable = false } = {}) {
  const storage = new Map(saved === null ? [] : [[THEME_STORAGE_KEY, saved]]);
  const systemListeners = new Set(), storageListeners = new Set(), applied = [];
  const media = {
    matches: dark,
    addEventListener: (_, listener) => systemListeners.add(listener),
    removeEventListener: (_, listener) => systemListeners.delete(listener),
  };
  const document = { documentElement: { dataset: {}, style: {} } };
  const window = {
    matchMedia: () => media,
    localStorage: {
      getItem: key => { if (unavailable) throw new Error('blocked'); return storage.get(key) ?? null; },
      setItem: (key, value) => { if (unavailable) throw new Error('blocked'); storage.set(key, value); },
    },
    addEventListener: (_, listener) => storageListeners.add(listener),
    removeEventListener: (_, listener) => storageListeners.delete(listener),
  };
  const controller = createThemeController({ window, document, onApply: value => applied.push(value) });
  return {
    controller, document, window, storage, applied,
    state: () => document.documentElement.dataset,
    os(dark) { media.matches = dark; systemListeners.forEach(listener => listener({ matches: dark })); },
    elsewhere(value) { if (value === null) storage.delete(THEME_STORAGE_KEY); else storage.set(THEME_STORAGE_KEY, value); storageListeners.forEach(listener => listener({ key: THEME_STORAGE_KEY, newValue: value })); },
    listenerCounts: () => [systemListeners.size, storageListeners.size],
  };
}

test('未设置时跟随系统；系统改变同时更新页面和原生控件颜色', () => {
  const env = environment({ dark: true });
  assert.deepEqual(env.state(), { theme: 'dark', themeMode: 'system' });
  assert.equal(env.document.documentElement.style.colorScheme, 'dark');
  env.os(false);
  assert.deepEqual(env.state(), { theme: 'light', themeMode: 'system' });
  assert.equal(env.document.documentElement.style.colorScheme, 'light');
});
test('手动主题持久化且不被系统改变覆盖，切回跟随系统立即使用当前系统设置', () => {
  const env = environment({ saved: 'dark' });
  assert.equal(env.state().theme, 'dark');
  env.os(false);
  assert.equal(env.state().theme, 'dark');
  env.controller.setMode('light');
  env.os(true);
  assert.equal(env.state().theme, 'light');
  assert.equal(env.storage.get(THEME_STORAGE_KEY), 'light');
  env.controller.setMode('system');
  assert.equal(env.state().theme, 'dark');
});
test('其他标签页改变/清除显示偏好时同步，未关联任何账户信息', () => {
  const env = environment({ dark: true, saved: 'light' });
  env.elsewhere('dark');
  assert.equal(env.state().themeMode, 'dark');
  env.elsewhere(null);
  assert.deepEqual(env.state(), { theme: 'dark', themeMode: 'system' });
  assert.deepEqual([...env.storage.keys()], []);
});
test('禁用浏览器存储时仍可手动切换和使用系统主题，不阻断网站', () => {
  const env = environment({ unavailable: true, dark: true });
  assert.equal(env.state().theme, 'dark');
  assert.equal(env.controller.setMode('light'), true);
  assert.equal(env.state().theme, 'light');
  env.os(true);
  assert.equal(env.state().theme, 'light');
});
test('损坏偏好回到system，未知输入不会覆盖用户选择，销毁解除系统监听', () => {
  const env = environment({ saved: 'unexpected', dark: true });
  assert.equal(env.state().themeMode, 'system');
  assert.equal(env.controller.setMode('unknown'), false);
  env.controller.setMode('light');
  env.controller.destroy();
  assert.deepEqual(env.listenerCounts(), [0, 0]);
  env.os(true);
  assert.equal(env.state().theme, 'light');
});
test('页面head预启动在应用和CSS前执行，读取同一设备偏好并容忍禁用存储', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const boot = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(boot);
  assert.ok(html.indexOf('<script>') < html.indexOf('/src/main.js'));
  for (const saved of [null, 'dark', 'light', 'system', 'invalid']) {
    const env = environment({ dark: true, saved });
    const doc = { documentElement: { dataset: {}, style: {} } };
    vm.runInNewContext(boot, { document: doc, localStorage: env.window.localStorage, matchMedia: env.window.matchMedia });
    assert.deepEqual({ ...doc.documentElement.dataset }, { ...env.state() });
  }
  const doc = { documentElement: { dataset: {}, style: {} } };
  vm.runInNewContext(boot, { document: doc, localStorage: { getItem() { throw new Error('blocked'); } }, matchMedia: () => ({ matches: true }) });
  assert.equal(doc.documentElement.dataset.theme, 'dark');
});

test('图标一键切换到另一种外观，点回系统色即恢复跟随', () => {
  const env = environment({ dark: false });
  env.controller.toggle();
  assert.deepEqual(env.state(), { theme: 'dark', themeMode: 'dark' });
  env.controller.toggle();
  assert.deepEqual(env.state(), { theme: 'light', themeMode: 'system' });
  env.os(true);
  assert.deepEqual(env.state(), { theme: 'dark', themeMode: 'system' });
});
