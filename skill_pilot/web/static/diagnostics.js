/* Runs before the application so startup script failures are captured locally. */
(() => {
  const session = new URLSearchParams(location.search).get('token');
  let sent = 0;
  const sourceLocation = (error, event) => {
    if (event?.filename && event.lineno) {
      try {
        const file = new URL(event.filename, location.href).pathname.split('/').pop();
        if (/^(app|diagnostics)\.js$/.test(file)) return `${file}:${event.lineno}:${event.colno || 0}`;
      } catch { /* Try a promise stack when no resource URL is available. */ }
    }
    const match = String(error?.stack || '').match(/(?:^|[\/\s(])((?:app|diagnostics)\.js)(?:\?[^\s)]*?)?:(\d{1,6}):(\d{1,6})(?=[)\s]|$)/m);
    return match ? `${match[1]}:${match[2]}:${match[3]}` : '';
  };
  const capture = async (error, event) => {
    if (!session || sent >= 10 || error?.error_id) return;
    sent += 1;
    try {
      const response = await fetch('/api/diagnostics/client-error', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Skill-Pilot-Token': session},
        body: JSON.stringify({message: String(error?.message || error || '页面脚本异常').slice(0, 4000),
          error_type: String(error?.name || 'Error'),
          location: sourceLocation(error, event)})
      });
      if (!response.ok) return;
      const result = await response.json();
      window.dispatchEvent(new CustomEvent('diagnostic-error', {detail: result}));
    } catch { /* Recording a browser error must never cause another one. */ }
  };
  window.addEventListener('error', event => capture(event.error || event.message, event));
  window.addEventListener('unhandledrejection', event => capture(event.reason));
})();
