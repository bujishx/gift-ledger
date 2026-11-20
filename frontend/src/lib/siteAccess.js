// The web server identifies the entry used for this request. Missing or invalid
// capability information must leave the business app usable without local tools.
export async function readMaintenanceAccess() {
  try {
    const response = await fetch('/site-access.json', { cache: 'no-store', redirect: 'error' });
    return response.ok && (await response.json()).maintenance === true;
  } catch {
    return false;
  }
}
