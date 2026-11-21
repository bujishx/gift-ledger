import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { createSSRApp, reactive } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { readMaintenanceAccess } from '../src/lib/siteAccess.js';
import { loadSiteAbout } from '../public/maintenance/site-info.js';

test('all three About views place the public repository below the version', async t => {
  function node(tag) {
    return {
      tag, children: [], classList: { add() {} },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this[name] = value; },
    };
  }
  globalThis.document = {
    createElement: node,
    createElementNS: (_, tag) => node(tag),
  };
  t.after(() => { delete globalThis.document; });
  const fetch = t.mock.method(globalThis, 'fetch');
  for (const [endpoint, info] of [
    ['/api/v1/site/about', { product: '往来有礼', version: '1.0' }],
    ['/api/v1/site/about', { project: 'example-site', version: '1.0' }],
    ['/api/v1/maintenance/about', { project: 'example-site', version: '1.0' }],
  ]) {
    fetch.mock.mockImplementation(async () => Response.json(info));
    const container = node('div');
    await loadSiteAbout(container, { endpoint });
    const rows = container.children.find(el => el.tag === 'dl').children;
    const labels = rows.map(row => row.children[0].textContent);
    assert.deepEqual(labels.slice(0, 2), ['网站版本', 'GitHub 项目']);
    const link = rows[1].children[1].children[0];
    assert.equal(link.tag, 'a');
    assert.equal(link.textContent, 'bujishx/gift-ledger');
    assert.equal(link.href, 'https://github.com/bujishx/gift-ledger');
    assert.equal(link.target, '_blank');
    assert.match(link.rel, /noopener/);
    assert.equal(link.children[0].children[0].href, '/maintenance/link-icons.svg#new-tab');
    if (info.product) assert.equal(rows.length, 2);
    else assert.equal(rows[labels.indexOf('本站项目')].children[1].children[0], 'example-site');
  }
});

test('maintenance controls require a fresh positive response from the current entry', async t => {
  const fetch = t.mock.method(globalThis, 'fetch');
  for (const [json, status, expected] of [
    [{ maintenance: true }, 200, true],
    [{ maintenance: false }, 200, false],
    [{ maintenance: 'true' }, 200, false],
    [{ maintenance: true }, 404, false],
  ]) {
    fetch.mock.mockImplementation(async (url, options) => {
      assert.equal(url, '/site-access.json');
      assert.equal(options.cache, 'no-store');
      assert.equal(options.redirect, 'error');
      return Response.json(json, { status });
    });
    assert.equal(await readMaintenanceAccess(), expected);
  }
  fetch.mock.mockImplementation(async () => { throw new TypeError('offline'); });
  assert.equal(await readMaintenanceAccess(), false);
});

test('login, About and personal data render tools for the local entry only', async t => {
  globalThis.window = { innerHeight: 800, innerWidth: 1200 };
  t.after(() => { delete globalThis.window; });
  const vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
    optimizeDeps: { noDiscovery: true, include: [] },
  });
  t.after(() => vite.close());
  async function render(file, props) {
    const { default: component } = await vite.ssrLoadModule(file);
    const context = {};
    const html = await renderToString(createSSRApp(component, reactive(props)), context);
    return html + (context.teleports?.body || '');
  }
  for (const maintenanceAvailable of [false, true]) {
    const login = await render('/src/pages/AuthPage.vue', {
      maintenanceAvailable, actions: {}, state: {
        account: null, authMode: 'login', registrationMode: 'closed', credentials: {}, profileForm: {}, busy: false, error: '', notice: '',
      },
    });
    const about = await render('/src/components/SiteAbout.vue', { maintenanceAvailable });
    assert.match(login, /欢迎回来/);
    assert.doesNotMatch(login, /凭邀请码注册|立即注册/);
    assert.match(about, /关于往来有礼/);
    for (const html of [login, about]) assert.equal(html.includes('href="/maintenance"'), maintenanceAvailable);
    const state = { meSection: 'data', profileForm: {}, busy: false };
    const props = { maintenanceAvailable, state, actions: {} };
    const personalData = await render('/src/pages/MePage.vue', props);
    assert.match(personalData, /导出全部数据/);
    assert.equal(personalData.includes('下载个人账本备份'), maintenanceAvailable);
    const tabs = personalData.match(/<button\b[^>]*role="tab"[^>]*>[\s\S]*?<\/button>/g);
    assert.equal(tabs.length, 4);
    assert.match(tabs[1], /旧账导入/);
    assert.match(tabs[2], /备份恢复/);
    assert.match(tabs[3], /字典管理/);
    assert.equal(tabs[2].includes('disabled'), !maintenanceAvailable);
    assert.equal(tabs[2].includes('仅限内网'), !maintenanceAvailable);
    state.meSection = 'restore';
    Object.assign(state, { restoreFile: null, restorePreview: null, restoreAcknowledged: false, restoreRenames: [] });
    const restore = await render('/src/pages/MePage.vue', props);
    assert.equal(restore.includes('恢复个人备份'), maintenanceAvailable);
    if (!maintenanceAvailable) assert.match(restore, /导出全部数据/);
  }
  const authProps = {maintenanceAvailable: false, actions: {}, state: {
    account:null, authMode:'login', registrationMode:'invite', inviteCode:'', credentials:{},
    profileForm:{}, busy:false, error:'', notice:'',
  }};
  assert.match(await render('/src/pages/AuthPage.vue', authProps), /凭邀请码注册/);
  authProps.state.authMode = 'register';
  assert.match(await render('/src/pages/AuthPage.vue', authProps), /name="invite_code"/);
  authProps.state.registrationMode = 'open';
  assert.doesNotMatch(await render('/src/pages/AuthPage.vue', authProps), /name="invite_code"/);
  authProps.state.authMode = 'login';
  assert.match(await render('/src/pages/AuthPage.vue', authProps), /立即注册/);
});
