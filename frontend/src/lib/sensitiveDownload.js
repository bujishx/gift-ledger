export function downloadFilename(disposition, fallback) {
  let name = fallback;
  const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition?.match(/filename="([^"]+)"|filename=([^;]+)/i);
  try { name = encoded ? decodeURIComponent(encoded) : plain ? (plain[1] || plain[2]).trim() : fallback; } catch { name = fallback; }
  return String(name).replace(/[\\/\x00-\x1f\x7f]/g, '_').slice(0, 180) || fallback;
}

// 凭据只通过请求头传递；先拿到完整文件，由调用者确认当前登录仍一致后再保存。
export async function fetchSensitiveDownload(url, grant, csrf, fallback, fetcher = fetch) {
  if (!grant || !csrf) throw new Error('请重新验证当前账号后下载');
  let response;
  try {
    response = await fetcher(url, { method: 'GET', credentials: 'include', headers: { 'X-Operation-Grant': grant, 'X-CSRF-Token': csrf } });
  } catch { throw new Error('暂时无法连接服务器，请重新验证后重试'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.error?.message || `下载失败（${response.status}）`);
    error.status = response.status; error.code = data.error?.code;
    throw error;
  }
  const blob = await response.blob();
  return { blob, filename: downloadFilename(response.headers.get('Content-Disposition'), fallback) };
}

export function saveSensitiveDownload({ blob, filename }) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = filename; anchor.hidden = true;
  document.body.append(anchor);
  try { anchor.click(); } finally { anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
}
