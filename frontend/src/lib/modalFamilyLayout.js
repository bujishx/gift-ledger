// 三个业务页的详情、筛选与录入窗口共用手机布局。
const controllers = new Set();
const background = ['.main-area', '.sidebar', '.bottom-nav'];

export function phoneModalFamily() { return window.innerWidth <= 700; }

export function allowModalBackdropClose(event) {
  if (!phoneModalFamily() || document.documentElement.hasAttribute('data-modal-family-keyboard')) return true;
  const rect = document.querySelector('.bottom-nav')?.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.height <= 0) return true;
  // 筛选和录入时，灰色导航属于禁用背景，点击不关闭当前任务。
  return !(event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom);
}

function syncBackground() {
  const task = [...controllers].some(item => item.active && item.kind !== 'detail');
  const keyboard = [...controllers].some(item => item.active && item.keyboard && phoneModalFamily());
  document.documentElement.toggleAttribute('data-modal-family-task', task);
  document.documentElement.toggleAttribute('data-modal-family-keyboard', keyboard);
  // 对应 App 的详情背景规则；关闭叠加表单时，仍打开的详情继续锁住正文。
  const detail = !!document.querySelector('.drawer-backdrop .person-drawer');
  for (const selector of background) {
    const element = document.querySelector(selector);
    if (element) element.inert = task || (detail && (selector === '.main-area' || !phoneModalFamily()));
  }
}

function navHeight() {
  const rect = document.querySelector('.bottom-nav')?.getBoundingClientRect();
  return rect?.height > 0 ? rect.height : 0;
}

function normalRowGap() {
  const rows = [...document.querySelectorAll('.mobile-list .mobile-card')]
    .map(element => element.getBoundingClientRect()).filter(rect => rect.height > 0 && rect.width > 0);
  const row = rows.find(rect => rect.bottom > 0 && rect.top < window.innerHeight) || rows[0];
  return row ? row.height / 2 : 40;
}

export function modalFamilyLayout(getPanel, { kind, getPosition = () => null } = {}) {
  let frame = 0, lockedGap = 40, savedNavHeight = 0, baselineHeight = window.innerHeight, lastWidth = window.innerWidth;
  const item = {
    kind, active: false, keyboard: false,
    update() {
      const panel = getPanel();
      if (!panel || !item.active) return;
      // 转屏或浏览器改宽时重置基准，避免把设备变化识别为软键盘。
      if (lastWidth !== window.innerWidth) {
        lastWidth = window.innerWidth;
        baselineHeight = window.innerHeight;
        lockedGap = normalRowGap();
      } else baselineHeight = Math.max(baselineHeight, window.innerHeight);
      if (!phoneModalFamily()) {
        panel.style.removeProperty('--family-bottom');
        panel.style.removeProperty('--family-max-height');
        item.keyboard = false;
        syncBackground();
        return;
      }
      const viewport = window.visualViewport;
      const visibleHeight = viewport?.height || window.innerHeight;
      const visibleOffset = viewport?.offsetTop || 0;
      const focused = panel.contains(document.activeElement) && document.activeElement.matches('input,textarea,select,[contenteditable="true"]');
      item.keyboard = focused && baselineHeight - visibleHeight > 120;
      const position = getPosition();
      const measuredNav = navHeight();
      if (measuredNav > 0) savedNavHeight = measuredNav;
      const gap = kind === 'detail' && Number(position?.cardGap) > 0 ? Number(position.cardGap) : lockedGap;
      const normalBottom = (kind === 'detail' && Number(position?.navHeight) > 0 ? Number(position.navHeight) : savedNavHeight) + gap;
      const overlap = Math.max(0, window.innerHeight - visibleHeight - visibleOffset);
      const bottom = item.keyboard ? overlap + 16 : normalBottom;
      const maxHeight = item.keyboard
        ? Math.max(1, visibleHeight - 32)
        : Math.max(1, Math.min(visibleHeight * .82, visibleHeight - bottom - 16));
      panel.style.setProperty('--family-bottom', `${bottom}px`);
      panel.style.setProperty('--family-max-height', `${maxHeight}px`);
      syncBackground();
      if (item.keyboard && focused) {
        requestAnimationFrame(() => {
          const field = document.activeElement;
          const body = panel.querySelector('.modal-body,.filter-dialog-body,.detail-card-body');
          if (!item.active || !body || !panel.contains(field)) return;
          const fieldRect = field.getBoundingClientRect(), bodyRect = body.getBoundingClientRect();
          if (fieldRect.top < bodyRect.top + 8) body.scrollTop -= bodyRect.top + 8 - fieldRect.top;
          else if (fieldRect.bottom > bodyRect.bottom - 8) body.scrollTop += fieldRect.bottom - bodyRect.bottom + 8;
        });
      }
    },
    schedule() {
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => { frame = 0; item.update(); });
    },
    start() {
      if (item.active) return;
      item.active = true;
      baselineHeight = window.innerHeight;
      lastWidth = window.innerWidth;
      savedNavHeight = navHeight();
      // 只在打开／转屏时取普通条目高度，实时筛选改变结果不会让窗口跳动。
      lockedGap = normalRowGap();
      controllers.add(item);
      window.addEventListener('resize', item.schedule);
      window.addEventListener('modal-family-layout', item.schedule);
      window.visualViewport?.addEventListener('resize', item.schedule);
      window.visualViewport?.addEventListener('scroll', item.schedule);
      document.addEventListener('focusin', item.schedule);
      document.addEventListener('focusout', item.schedule);
      item.update();
    },
    stop() {
      if (!item.active) return;
      item.active = item.keyboard = false;
      controllers.delete(item);
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      window.removeEventListener('resize', item.schedule);
      window.removeEventListener('modal-family-layout', item.schedule);
      window.visualViewport?.removeEventListener('resize', item.schedule);
      window.visualViewport?.removeEventListener('scroll', item.schedule);
      document.removeEventListener('focusin', item.schedule);
      document.removeEventListener('focusout', item.schedule);
      const panel = getPanel();
      panel?.style.removeProperty('--family-bottom');
      panel?.style.removeProperty('--family-max-height');
      syncBackground();
    },
  };
  return item;
}
