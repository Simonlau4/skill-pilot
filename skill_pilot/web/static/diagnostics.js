/* Runs before the application so startup script failures are captured locally. */
(() => {
  const session = new URLSearchParams(location.search).get('token');
  let sent = 0;
  const capture = async error => {
    if (!session || sent >= 10 || error?.error_id) return;
    sent += 1;
    try {
      const response = await fetch('/api/diagnostics/client-error', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Skill-Pilot-Token': session},
        body: JSON.stringify({message: String(error?.message || error || '页面脚本异常').slice(0, 4000),
          error_type: String(error?.name || 'Error'),
          location: String(error?.stack || '').match(/(?:app|diagnostics)\.js:\d+:\d+/)?.[0] || ''})
      });
      if (!response.ok) return;
      const result = await response.json();
      window.dispatchEvent(new CustomEvent('diagnostic-error', {detail: result}));
    } catch { /* Recording a browser error must never cause another one. */ }
  };
  window.addEventListener('error', event => capture(event.error || event.message));
  window.addEventListener('unhandledrejection', event => capture(event.reason));
})();
