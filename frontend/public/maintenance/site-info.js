// Both About dialogs render the same projection. Each page supplies its own authorized endpoint.
export function displayVersion(value) {
  return /^[0-9]/.test(value) ? 'v' + value : (value || '未提供');
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function row(list, label, value) {
  const pair = element('div');
  const detail = element('dd');
  detail.append(value || '未提供');
  pair.append(element('dt', label), detail);
  list.append(pair);
}

function verification(info) {
  if (info.restore_state === 'none') return '尚未读到此实例已完成的整站恢复回执。';
  if (info.restore_state !== 'verified') return '暂时无法核对本实例的整站恢复记录。';
  if (!info.restored_at) return '本实例已完成整站恢复核验，原回执未提供时间。';
  const stamp = new Date(info.restored_at);
  if (Number.isNaN(stamp.getTime())) return '本实例已完成整站恢复核验，原回执未提供时间。';
  return '本实例于 ' + new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short'}).format(stamp) + ' 完成整站恢复核验。';
}

export async function loadSiteAbout(container, {endpoint, token, signal} = {}) {
  container.classList.add('site-about-content');
  const brand = element('div', '', 'site-about-brand');
  const image = element('img');
  image.src = '/brand-mark.png';
  image.alt = '';
  image.width = image.height = 48;
  brand.append(image, element('strong', '往来有礼'), element('p', '记人情往来，守礼数分寸'));
  const progress = element('p', '正在核对本站信息…', 'site-about-note');
  progress.setAttribute('role', 'status');
  container.replaceChildren(brand, progress);
  try {
    const headers = token ? {Authorization: 'Bearer ' + token} : {};
    const response = await fetch(endpoint, {headers, credentials: 'same-origin', cache: 'no-store', signal});
    if (!response.ok) throw new Error();
    const info = await response.json();
    if (signal?.aborted) return;
    const list = element('dl', '', 'site-about-rows');
    const details = element('details', '', 'site-about-details');
    details.append(element('summary', '查看完整标识'));
    const full = element('dl', '', 'site-about-rows');
    details.append(full);
    const feedback = element('span', '', 'site-about-feedback');
    feedback.setAttribute('role', 'status');
    function shortValue(value, count, name) {
      if (!value) return null;
      const button = element('button', value.slice(0, count), 'site-about-copy');
      button.type = 'button';
      button.title = '复制完整' + name;
      button.setAttribute('aria-label', button.title);
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS(icon.namespaceURI, 'path');
      path.setAttribute('d', 'M8 8h12v13H8z M16 8V4H4v13h4');
      icon.append(path);
      button.append(icon);
      button.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(value);
          feedback.textContent = name + '已复制';
        } catch {
          details.open = true;
          feedback.textContent = '请在完整标识中选中文字复制。';
        }
      });
      return button;
    }
    row(list, '网站版本', displayVersion(info.version));
    row(list, '更新日期', info.updated_at);
    row(list, info.source_snapshot ? '基础 Git 提交' : 'Git 提交', shortValue(info.git_commit, 12, ' Git 提交号'));
    row(list, '本站项目', info.project);
    row(list, '本站实例', shortValue(info.instance_id, 8, '站点实例'));
    if (info.source_snapshot) row(list, '候选源码', shortValue(info.source_snapshot, 12, '候选源码摘要'));
    row(full, '本站网址', info.origin);
    row(full, '本站实例', info.instance_id);
    row(full, 'Git 提交', info.git_commit);
    if (info.source_snapshot) row(full, '候选源码 SHA-256', info.source_snapshot);
    container.replaceChildren(brand, list, element('p', verification(info), 'site-about-note'), details, feedback);
  } catch {
    if (!signal?.aborted) progress.textContent = '暂时无法读取本站信息。请确认登录状态后重新打开。';
  }
}
