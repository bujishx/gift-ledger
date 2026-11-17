/**
 * 查账／查事／悬浮卡的纯展示与参数逻辑。
 *
 * 这里刻意不依赖 Vue 与浏览器全局，方便用 Node 内置测试直接覆盖
 * 与 API_CONTRACT.md 相关的关键行为。
 */

/** F01 查账搜索范围完整文案。 */
export const GIFT_SEARCH_SCOPE =
  "搜索目标：事主姓名或备注、事件备注、随礼人姓名或备注；用空格分隔关键词，匹配其中任意一个";

/** F03 礼金方向单字。 */
export function giftMark(direction) {
  return direction === "expense" ? "支" : "收";
}

/** F03 礼金颜色类：支出红、收入绿。 */
export function giftClass(direction) {
  return direction === "expense" ? "expense" : "income";
}

/** F03 随礼人：姓名(关系类型·联系人备注)，用于区分同名人。 */
export function queryContactLabel(contact) {
  if (!contact) return "—";
  const detail = [contact.relationship_type?.name, contact.remark]
    .filter(Boolean)
    .join("·");
  return detail ? `${contact.name}(${detail})` : contact.name;
}

/** 只有服务端明确要求密码再验证时才弹密码框。 */
export function isReauthError(err) {
  return err?.code === "GIFT_CHANGE_REAUTH_REQUIRED";
}

/** 查账临时精确筛选：事件 ID 或联系人 ID，两者互斥。 */
export function ledgerContextParams(context) {
  if (!context) return { event_id: "", contact_id: "" };
  if (context.kind === "statistics") return {...context.params};
  return {
    event_id: context.kind === "event" ? context.id : "",
    contact_id: context.kind === "contact" ? context.id : "",
  };
}

/** 查事临时精确筛选：事主联系人 ID。 */
export function eventContextParams(context) {
  return { host_contact_id: context?.id || "" };
}

/** 把卡片约束在视口内。 */
export function clampCardPosition(left, top, size, viewport, margin = 8) {
  const maxLeft = Math.max(margin, viewport.width - size.width - margin);
  const maxTop = Math.max(margin, viewport.height - size.height - margin);
  return {
    left: Math.min(Math.max(left, margin), maxLeft),
    top: Math.min(Math.max(top, margin), maxTop),
  };
}

/**
 * 在锚点元素旁给出卡片位置：优先右侧、其次左侧，放不下再退回锚点左侧；
 * 纵向优先与锚点齐平，底部空间不足时改为与锚点底部对齐。最后统一约束进视口。
 */
export function pickCardPosition(anchor, size, viewport, margin = 8) {
  if (!anchor) {
    return clampCardPosition(
      (viewport.width - size.width) / 2,
      Math.max(margin, (viewport.height - size.height) / 2),
      size,
      viewport,
      margin,
    );
  }
  const left =
    anchor.right + size.width + 9 <= viewport.width - margin
      ? anchor.right + 9
      : anchor.left - size.width - 9 >= margin
        ? anchor.left - size.width - 9
        : anchor.left;
  const top =
    anchor.top + size.height <= viewport.height - margin
      ? anchor.top
      : anchor.bottom - size.height;
  return clampCardPosition(left, top, size, viewport, margin);
}
