/**
 * 查询串构造。空串、null、undefined 一律跳过，数组按重复键展开。
 * 与后端 FastAPI 的多值 Query 参数约定一致。
 */
export function queryString(values = {}) {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === "" || value == null) continue;
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== "" && item != null) result.append(key, String(item));
    }
  }
  const text = result.toString();
  return text ? `?${text}` : "";
}
