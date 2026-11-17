import { queryString } from "./lib/query";
import { fetchSensitiveDownload } from './lib/sensitiveDownload.js';

export { queryString };

const BASE = import.meta.env.VITE_API_BASE || "/api/v1";

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)gift_ledger_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export function sensitiveDownload(path, grant, fallback) {
  return fetchSensitiveDownload(`${BASE}${path}`, grant, csrfToken(), fallback);
}

export async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
    });
  } catch {
    throw new Error("暂时无法连接服务器，请检查服务是否已启动");
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      data.error?.message || `请求失败（${response.status}）`,
    );
    error.status = response.status;
    error.code = data.error?.code;
    error.details = data.error?.details;
    throw error;
  }
  return data;
}

export async function upload(path, file, fields = {}) {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined) form.append(key, typeof value === 'string' ? value : JSON.stringify(value));
  }
  const token = csrfToken();
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method: "POST",
      body: form,
      headers: token ? { "X-CSRF-Token": token } : {},
      credentials: "include",
    });
  } catch {
    throw new Error("暂时无法连接服务器，请检查服务是否已启动");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      data.error?.message || `请求失败（${response.status}）`,
    );
    error.status = response.status;
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

export async function allPages(path, params = {}, maxPages = 100) {
  const items = [];
  for (let page = 0; page < maxPages; page += 1) {
    const data = await api(
      `${path}${queryString({ ...params, limit: 100, offset: page * 100 })}`,
    );
    items.push(...(data.items || []));
    const total = data.total ?? data.page?.total ?? items.length;
    if (items.length >= total || (data.items || []).length < 100) break;
  }
  return items;
}
