import { nextTick, reactive, ref } from "vue";
import { clampCardPosition, pickCardPosition } from "../lib/ledgerView.js";

/**
 * 查人／查事／查账三处悬浮卡共用的定位与拖动行为。
 *
 * - 电脑（>1100px）：贴着触发元素就近弹出，并可按住卡头拖动。
 * - 平板（701–1100px）：居中，不拖动。
 * - 手机（<=700px）：距实际底部导航半条目标列表项高度，不拖动。
 * DetailCard 在自身根层绑定 navHeight/cardGap；不写全局 CSS 变量。
 */
export function useFloatingCard(margin = 8) {
  const cardRef = ref(null);
  const position = reactive({ left: 12, top: 12, navHeight: 0, cardGap: 0 });
  const drag = { id: null, x: 0, y: 0, left: 0, top: 0, target: null };
  let previousFocus;
  let anchorRect = null, anchorElement = null;
  let pendingRect = null, pendingElement = null;
  let refTarget = null, lastMode = null, layoutRequest = 0;

  function elementFrom(target) {
    const element = target?.card?.value || target?.card || target;
    return typeof element?.getBoundingClientRect === "function" ? element : null;
  }

  function cardElement() {
    const element = elementFrom(refTarget);
    cardRef.value = element;
    return element;
  }

  function clearDrag() {
    try {
      if (drag.id !== null && (!drag.target?.hasPointerCapture || drag.target.hasPointerCapture(drag.id)))
        drag.target?.releasePointerCapture?.(drag.id);
    } catch { /* 关闭卡片时指针捕获可能已由浏览器释放。 */ }
    drag.id = null;
    drag.target = null;
  }

  function setRef(element) {
    refTarget = element;
    cardRef.value = elementFrom(element);
    if (!element) {
      layoutRequest++;
      clearDrag();
      anchorRect = anchorElement = pendingRect = pendingElement = null;
      previousFocus = null;
      lastMode = null;
      position.navHeight = position.cardGap = 0;
    }
  }

  function viewport() {
    return { width: window.innerWidth, height: window.innerHeight };
  }

  function mode() {
    return window.innerWidth <= 700 ? "phone" : window.innerWidth <= 1100 ? "tablet" : "desktop";
  }

  function visibleRect(element) {
    if (!element?.isConnected) return null;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 ? rect : null;
  }

  function currentAnchor() {
    const current = visibleRect(anchorElement);
    if (current) return (anchorRect = current);
    // 程序化按联系人 ID 打开、或跨设备宽度后，用当前目标列表项重测。
    const selector = mode() === "phone" ? ".mobile-list .mobile-card.selected" : ".data-table tr.selected";
    for (const element of document.querySelectorAll(selector)) {
      const rect = visibleRect(element);
      if (rect) { anchorElement = element; return (anchorRect = rect); }
    }
    return anchorRect;
  }

  function updateSpacing() {
    const rect = currentAnchor();
    if (mode() !== "phone") {
      position.navHeight = position.cardGap = 0;
      return;
    }
    const nav = document.querySelector(".bottom-nav")?.getBoundingClientRect();
    position.navHeight = nav?.height > 0 ? Math.max(0, window.innerHeight - nav.top) : 0;
    const height = rect?.height ?? (rect ? rect.bottom - rect.top : 0);
    position.cardGap = Math.max(0, Number(height) || 0) / 2;
  }

  function clamp(left, top) {
    const card = cardElement();
    if (!card) return;
    const next = clampCardPosition(
      left,
      top,
      { width: card.offsetWidth, height: card.offsetHeight },
      viewport(),
      margin,
    );
    position.left = next.left;
    position.top = next.top;
  }

  function positionCard(initial) {
    const card = cardElement();
    if (!card) return;
    const size = { width: card.offsetWidth, height: card.offsetHeight };
    const view = viewport(), currentMode = mode();
    let next;
    if (currentMode === "phone") {
      next = {
        left: Math.max(margin, (view.width - size.width) / 2),
        top: Math.max(margin, view.height - position.navHeight - position.cardGap - size.height),
      };
    } else if (currentMode === "tablet") {
      next = pickCardPosition(null, size, view, margin);
    } else if (initial || lastMode !== currentMode) {
      next = pickCardPosition(currentAnchor(), size, view, margin);
    } else {
      next = clampCardPosition(position.left, position.top, size, view, margin);
    }
    position.left = next.left;
    position.top = next.top;
    lastMode = currentMode;
  }

  /** 接受原 DOMRect 接口；先让响应式高度限制生效，再定位与聚焦。 */
  async function place(anchor) {
    const request = ++layoutRequest;
    previousFocus = document.activeElement;
    anchorRect = anchor || null;
    anchorElement = anchor === pendingRect ? pendingElement : null;
    pendingRect = pendingElement = null;
    clearDrag();
    await nextTick();
    if (request !== layoutRequest || !cardElement()) return;
    updateSpacing();
    await nextTick();
    if (request !== layoutRequest) return;
    const card = cardElement();
    if (!card) return;
    positionCard(true);
    card.focus({ preventScroll: true });
  }

  function restoreFocus() {
    const target = previousFocus;
    previousFocus = null;
    nextTick(() => {
      if (target?.isConnected && !target.disabled && !target.closest?.("[inert]"))
        target.focus({ preventScroll: true });
    });
  }

  /** 从点击事件取锚点矩形。 */
  function anchorFrom(event) {
    const target = event?.currentTarget;
    pendingElement = target?.getBoundingClientRect ? target : null;
    pendingRect = pendingElement?.getBoundingClientRect() || null;
    return pendingRect;
  }

  function startDrag(event) {
    if (
      window.innerWidth <= 1100 ||
      event.button !== 0 ||
      event.target.closest?.("button")
    )
      return;
    drag.id = event.pointerId;
    drag.x = event.clientX;
    drag.y = event.clientY;
    drag.left = position.left;
    drag.top = position.top;
    drag.target = event.currentTarget;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  }

  function moveDrag(event) {
    if (event.pointerId !== drag.id) return;
    if (mode() !== "desktop") { clearDrag(); return; }
    clamp(
      drag.left + event.clientX - drag.x,
      drag.top + event.clientY - drag.y,
    );
  }

  function stopDrag(event) {
    if (event.pointerId === drag.id) clearDrag();
  }

  function keepInView() {
    if (!cardElement()) return;
    const request = ++layoutRequest;
    if (mode() !== "desktop") clearDrag();
    updateSpacing();
    nextTick(() => {
      if (request === layoutRequest && cardElement()) positionCard(false);
    });
  }

  return {
    cardRef,
    position,
    setRef,
    place,
    restoreFocus,
    anchorFrom,
    startDrag,
    moveDrag,
    stopDrag,
    keepInView,
  };
}
