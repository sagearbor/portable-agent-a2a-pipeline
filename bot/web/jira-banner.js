/* =========================================================================
   Jira connection banner — shared by every page (index.html, about.html).

   Polls GET api/v1/jira/health on load and every 10 min.  When the shared
   Jira credential is missing / invalid / expired it pins a big red bar
   across the top of the page; amber when the token expires within 14 days
   or Jira is unreachable.  The bar can be collapsed to a thin strip, but the
   collapse is NOT remembered — it comes back on every reload.

   The API URL is resolved relative to this script's own URL, so it works
   both at the site root (localhost:3006/) and behind a reverse-proxy subpath
   (…/sageapp06/), the same way the existing relative asset paths do.

   Exposes window.jiraBanner.refresh() so page code can re-check right after
   a Jira call fails.
========================================================================= */
(function () {
  'use strict';

  const POLL_MS = 10 * 60 * 1000;
  const HEALTH_URL = new URL('api/v1/jira/health', document.currentScript
    ? document.currentScript.src : window.location.href).toString();

  const log = (level, ...args) => {
    if (typeof window.dbg === 'function') window.dbg(level, ...args);
  };

  // level -> look.  "unreachable" deliberately does NOT say "token expired".
  const LOOK = {
    missing:       { tone: 'red',     title: 'Jira connection is down' },
    invalid:       { tone: 'red',     title: 'Jira connection is down' },
    expiring_soon: { tone: 'amber',   title: 'Jira token expiring soon' },
    unreachable:   { tone: 'neutral', title: 'Jira is not responding' },
  };

  const CSS = `
    :root { --jira-banner-h: 0px; }
    body { padding-top: var(--jira-banner-h) !important; }
    #sidebar { top: var(--jira-banner-h) !important; }
    #jira-banner {
      position: fixed; top: 0; left: 0; right: 0; z-index: 2147483000;
      display: flex; align-items: center; gap: 16px;
      padding: 16px 24px; font: 600 16px/1.4 'Segoe UI', system-ui, sans-serif;
      color: #fff; box-shadow: 0 3px 12px rgba(0,0,0,0.35);
    }
    #jira-banner[hidden] { display: none; }
    #jira-banner.tone-red     { background: #c62828; border-bottom: 4px solid #8e0000; }
    #jira-banner.tone-amber   { background: #b26a00; border-bottom: 4px solid #7a4800; }
    #jira-banner.tone-neutral { background: #4a5163; border-bottom: 4px solid #2d3142; }
    #jira-banner .jb-icon { font-size: 28px; line-height: 1; flex: none; }
    #jira-banner .jb-text { flex: 1; min-width: 0; }
    #jira-banner .jb-title { display: block; font-size: 20px; font-weight: 700; }
    #jira-banner .jb-msg { display: block; font-weight: 500; }
    #jira-banner button {
      flex: none; cursor: pointer; font: 600 13px 'Segoe UI', system-ui, sans-serif;
      color: #fff; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.6);
      border-radius: 6px; padding: 6px 12px;
    }
    #jira-banner button:hover { background: rgba(0,0,0,0.4); }
    #jira-banner.collapsed { padding: 6px 24px; }
    #jira-banner.collapsed .jb-msg, #jira-banner.collapsed .jb-icon { display: none; }
    #jira-banner.collapsed .jb-title { font-size: 14px; }
    @media (max-width: 640px) {
      #jira-banner { padding: 12px 16px; font-size: 14px; }
      #jira-banner .jb-title { font-size: 17px; }
    }
  `;

  let bar = null;
  let collapsed = false;   // per page-load only; never persisted

  function ensureBar() {
    if (bar) return bar;
    const style = document.createElement('style');
    style.id = 'jira-banner-style';
    style.textContent = CSS;
    document.head.appendChild(style);

    bar = document.createElement('div');
    bar.id = 'jira-banner';
    bar.setAttribute('role', 'alert');
    bar.setAttribute('aria-live', 'assertive');
    bar.hidden = true;
    bar.innerHTML =
      '<span class="jb-icon" aria-hidden="true">&#9888;</span>' +
      '<span class="jb-text"><strong class="jb-title"></strong>' +
      '<span class="jb-msg"></span></span>' +
      '<button type="button" class="jb-toggle"></button>';
    bar.querySelector('.jb-toggle').addEventListener('click', () => {
      collapsed = !collapsed;
      render();
    });
    document.body.insertBefore(bar, document.body.firstChild);
    // Keep the page content clear of the fixed bar when it wraps / resizes.
    if (window.ResizeObserver) new ResizeObserver(syncOffset).observe(bar);
    window.addEventListener('resize', syncOffset);
    return bar;
  }

  function syncOffset() {
    const h = bar && !bar.hidden ? bar.offsetHeight : 0;
    document.documentElement.style.setProperty('--jira-banner-h', h + 'px');
  }

  let current = null;

  function render() {
    const look = current && LOOK[current.status];
    if (!look) {
      if (bar) { bar.hidden = true; syncOffset(); }
      return;
    }
    ensureBar();
    bar.className = 'tone-' + look.tone + (collapsed ? ' collapsed' : '');
    bar.dataset.status = current.status;
    bar.querySelector('.jb-title').textContent =
      collapsed ? look.title + ' — ticket creation affected' : look.title;
    // Don't print the title twice when the message opens with it.
    let msg = current.message || '';
    if (msg.startsWith(look.title)) msg = msg.slice(look.title.length).replace(/^\s*[—-]\s*/, '');
    bar.querySelector('.jb-msg').textContent = msg.charAt(0).toUpperCase() + msg.slice(1);
    const btn = bar.querySelector('.jb-toggle');
    btn.textContent = collapsed ? 'Show details' : 'Collapse';
    btn.setAttribute('aria-expanded', String(!collapsed));
    bar.hidden = false;
    syncOffset();
  }

  async function refresh() {
    try {
      const resp = await fetch(HEALTH_URL, { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      current = await resp.json();
      log(5, '[jira-banner] status:', current.status);
    } catch (e) {
      // The app itself is unreachable — leave the last known state alone.
      log(1, '[jira-banner] health check failed:', e.message);
      return;
    }
    render();
  }

  window.jiraBanner = { refresh };

  function start() {
    refresh();
    setInterval(refresh, POLL_MS);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
