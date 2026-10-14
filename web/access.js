"use strict";
// Safari BFCache reload (commit 40358133). Lives here, not inline: /access CSP forbids inline script.
window.addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function safeSlice(str, start, end, fallback) {
  if (typeof str !== 'string' || str.length < end) return fallback !== undefined ? fallback : '';
  return str.slice(start, end);
}

function parseUA(ua){
  if(!ua) return '';
  let browser='', os='';
  const cm=ua.match(/Chrome\/(\d+)/);
  const fm=ua.match(/Firefox\/(\d+)/);
  if(cm&&!ua.includes('Edg/'))      browser='Chrome\u00a0'+cm[1];
  else if(ua.includes('Edg/'))      browser='Edge';
  else if(fm)                       browser='Firefox\u00a0'+fm[1];
  else if(ua.includes('Mobile/'))   browser='Safari\u00a0Mobile';
  else if(ua.includes('Safari'))    browser='Safari';
  if(!browser){
    if(ua.match(/curl\//i))             browser='curl';
    else if(ua.match(/python-requests/i)) browser='python';
    else if(ua.match(/Go-http-client/i))  browser='Go';
    else if(ua.match(/Wget\//i))          browser='wget';
    else if(ua){ const m=ua.match(/compatible;\s*([A-Za-z][^;/)]{1,19})/i); browser=m?m[1].trim():ua.slice(0,24).replace(/[\s;,]+$/,''); }
  }
  if(ua.includes('iPhone')||ua.includes('iPad')) os='iOS';
  else if(ua.includes('Macintosh')) os='macOS';
  else if(ua.includes('Android'))   os='Android';
  else if(ua.includes('Linux'))     os='Linux';
  else if(ua.includes('Windows'))   os='Windows';
  return [browser,os].filter(Boolean).join('\u00a0\xb7\u00a0');
}

function pathCls(p){
  if(p==='/')                   return 'p-badge p-root';
  if(p.startsWith('/sessions')) return 'p-badge p-sessions';
  if(p.startsWith('/access'))   return 'p-badge p-access';
  return 'p-badge';
}

let today = new Date().toISOString().slice(0,10);  // updated from server_date on first fetch

function statusBadge(auth) {
  if (auth === 'BLOCKED')      return '<span class="badge badge-blocked">Blocked</span>';
  if (auth === 'TOKEN')        return '<span class="badge badge-token">Token</span>';
  if (auth === 'OK')           return '<span class="badge badge-ok">OK</span>';
  if (auth === 'RATE_LIMITED') return '<span class="badge badge-rate">429</span>';
  if (auth === 'MIXED') return '<span class="badge badge-mixed">Mixed</span>';
  return `<span class="badge badge-other">${esc(auth)}</span>`;
}

function cleanPtr(ptr) {
  // Strip leading IP-encoded prefix: '130-43-163-43.btcentralplus.com' -> 'btcentralplus.com'
  const parts = ptr.split('.');
  const i = parts.findIndex(p => /^[a-zA-Z]/.test(p));
  return i > 0 ? parts.slice(i).join('.') : ptr;
}

function hostDisplay(e) {
  const hasPtr    = e.rdns && e.rdns !== e.ip;
  const isPrivate = e.isp === 'doxxnet private' || e.isp === 'LAN';
  // Prefer org/ISP as primary label; PTR hostname is secondary (tooltip)
  const label  = isPrivate ? e.isp : (e.isp || (hasPtr ? cleanPtr(e.rdns) : ''));
  const hostCls = label ? (isPrivate ? 'td-rdns dim' : 'td-rdns') : 'td-rdns dim';
  const hostTxt = isPrivate
    ? `<span class="host-private">${esc(e.isp)}</span>`
    : label ? esc(label) : `<span class="host-missing">${esc(e.ip)}</span>`;
  // Tooltip: full PTR + org so neither is hidden
  const tipParts = [...new Set([e.isp, hasPtr && e.rdns].filter(Boolean))];
  const tooltip  = tipParts.join(' \xb7 ') || e.ip;
  return { hostCls, hostTxt, tooltip };
}

/**
 * @param {string} extraAttrs - MUST be static HTML attributes only, never server-derived content
 * @param {string} extraCls - additional CSS classes
 */
function rowToHtml(e, extraAttrs='', extraCls=''){
  const cls = (e.auth==='BLOCKED'||e.auth==='RATE_LIMITED')?'blocked':e.auth==='TOKEN'?'token':e.auth==='OPEN'?'open':'ok';
  const ua  = parseUA(e.ua)||'-';
  const { hostCls, hostTxt, tooltip } = hostDisplay(e);
  const loc = [e.city,e.country].filter(Boolean).join(', ')||'-';
  const datePrefix = safeSlice(e.ts,0,10,'')!==today
    ? `<span class="date-prefix">${safeSlice(e.ts,8,10,'??')+'-'+safeSlice(e.ts,5,7,'??')}\u00a0</span>` : '';
  return `<tr class="${cls}${extraCls ? ' ' + extraCls : ''}" ${extraAttrs}>
    <td class="td-time">${datePrefix}${esc(safeSlice(e.ts,11,19,'--:--:--'))}</td>
    <td>${statusBadge(e.auth)}</td>
    <td class="td-ip">${esc(e.ip)}</td>
    <td class="${hostCls}">${hostTxt}</td>
    <td class="td-loc">${esc(loc)}</td>
    <td class="td-path"><span class="td-path-text"><span class="${pathCls(e.path)}">${esc(e.path)}</span></span><span class="td-path-ua">${esc(ua)}</span></td>
  </tr>`;
}

let groupSeq = 0;

function groupRowsHtml(entries) {
  const gid   = ++groupSeq;
  const first = entries[0];  // newest
  const last  = entries[entries.length - 1]; // oldest
  const count = entries.length;

  const anyBlocked     = entries.some(e => e.auth === 'BLOCKED' || e.auth === 'RATE_LIMITED');
  const allOk          = entries.every(e => e.auth === 'OK');
  const anyToken       = entries.some(e => e.auth === 'TOKEN');
  const anyRateLimited = entries.some(e => e.auth === 'RATE_LIMITED');
  // Detect mixed: more than one distinct normalised auth type
  const normAuths = new Set(entries.map(e => e.auth === 'RATE_LIMITED' ? 'BLOCKED' : e.auth));
  const isMixed   = normAuths.size > 1;
  const cls = isMixed ? 'mixed' : anyBlocked ? 'blocked' : anyToken ? 'token' : allOk ? 'ok' : anyRateLimited ? 'blocked' : 'open';
  // Build status badge(s): mixed groups show one badge per distinct auth in first-seen order
  let statusBadgesHtml;
  if (isMixed) {
    const seenAuths = new Set();
    const badgeParts = [];
    for (const e of entries) {
      if (!seenAuths.has(e.auth)) {
        seenAuths.add(e.auth);
        badgeParts.push(statusBadge(e.auth));
      }
    }
    statusBadgesHtml = badgeParts.join('\u00a0');
  } else {
    const singleAuth = anyBlocked ? 'BLOCKED' : allOk ? 'OK' : anyToken ? 'TOKEN' : anyRateLimited ? 'RATE_LIMITED' : 'MIXED';
    statusBadgesHtml = statusBadge(singleAuth);
  }

  const { hostCls, hostTxt, tooltip } = hostDisplay(first);
  const loc = [first.city, first.country].filter(Boolean).join(', ') || '-';
  const ua  = parseUA(first.ua) || '-';

  const paths   = [...new Set(entries.map(e => e.path))];
  const pathTxt = paths.length === 1 ? paths[0] : paths[0] + ' +' + (paths.length - 1);
  const t1 = safeSlice(last.ts,11,16,'--:--');   // oldest HH:MM (no seconds keeps range within column width)
  const t2 = safeSlice(first.ts,11,16,'--:--');  // newest HH:MM
  const newestIsToday = safeSlice(first.ts,0,10,'') === today;
  const isCrossDay    = safeSlice(last.ts,0,10,'') !== today && newestIsToday;
  const datePrefix    = !newestIsToday
    ? `<span class="date-prefix">${safeSlice(last.ts,8,10,'??')+'-'+safeSlice(last.ts,5,7,'??')}\u00a0</span>` : '';
  const timeRange     = t1 === t2 ? safeSlice(first.ts,11,19,'--:--:--') : `${t1}\u2013${t2}`;

  const daysDiff      = isCrossDay ? Math.round((new Date(safeSlice(first.ts,0,10,'')) - new Date(safeSlice(last.ts,0,10,''))) / 86400000) : 0;
  const crossDayBadge = isCrossDay ? `<span class="cross-day-badge">+${daysDiff}d</span>` : '';

  const summaryRow = `<tr class="${cls} grp-summary" data-gid="${gid}" data-grp-ip="${esc(first.ip)}">
    <td class="td-time">${datePrefix}${esc(timeRange)}${crossDayBadge}</td>
    <td>${statusBadgesHtml}</td>
    <td class="td-ip">${esc(first.ip)}\u00a0<span class="grp-expand" id="ga-${gid}">+${count}</span></td>
    <td class="${hostCls}">${hostTxt}</td>
    <td class="td-loc">${esc(loc)}</td>
    <td class="td-path"><span class="td-path-text"><span class="${pathCls(paths[0])}">${esc(pathTxt)}</span></span><span class="td-path-ua">${esc(ua)}</span></td>
  </tr>`;

  const detailRows = entries.map(e =>
    rowToHtml(e, `data-grp="${gid}"`, 'grp-detail')
  ).join('');

  return summaryRow + detailRows;
}

function toggleGroup(gid) {
  const rows  = document.querySelectorAll(`[data-grp="${gid}"]`);
  const badge = document.getElementById('ga-' + gid);
  const isOpen = rows.length > 0 && getComputedStyle(rows[0]).display !== 'none';
  rows.forEach(r => { r.style.display = isOpen ? 'none' : ''; r.classList.remove('grp-detail'); });
  if (badge) badge.textContent = isOpen ? `+${rows.length}` : `\u2212${rows.length}`;
}

// ── Event delegation for group row toggling ───────────────────────────────────
document.addEventListener('click', function(e) {
  // Dashboard tab: rows with data-gid
  const dashRow = e.target.closest('[data-gid]');
  if (dashRow && dashRow.dataset.gid) {
    toggleGroup(parseInt(dashRow.dataset.gid, 10));
    return;
  }
});

let allRows    = [];
let filterAuth = 'all';
let filterIP   = '';
let filterOrigin = 'all';

function isDoxxnetEntry(e) {
  const isp = (e.isp || '').toLowerCase();
  return isp === 'doxxnet private' || isp.includes('doxx.net');
}
function isLanEntry(e) {
  const isp = (e.isp || '').toLowerCase();
  return isp === 'lan' || isp === 'rpi';
}

function applyFilters(){
  // Save which group IPs are currently expanded before rebuilding DOM
  const expandedIPs = new Set();
  document.querySelectorAll('.grp-summary[data-gid]').forEach(row => {
    const detail = document.querySelector(`[data-grp="${row.dataset.gid}"]`);
    if (detail && getComputedStyle(detail).display !== 'none') expandedIPs.add(row.dataset.grpIp);
  });

  groupSeq = 0;
  let rows = allRows.slice();
  if(filterAuth !== 'all') rows = rows.filter(e => e.auth === filterAuth);
  if (filterOrigin !== 'all') {
    if (filterOrigin === 'doxxnet') rows = rows.filter(e => isDoxxnetEntry(e));
    else if (filterOrigin === 'lan') rows = rows.filter(e => isLanEntry(e));
    else rows = rows.filter(e => !isDoxxnetEntry(e) && !isLanEntry(e));
  }
  if(filterIP){
    const q = filterIP.toLowerCase();
    rows = rows.filter(e => (e.ip||'').includes(q)||(e.rdns||'').toLowerCase().includes(q)||(e.isp||'').toLowerCase().includes(q));
  }
  // Group all entries per IP into one collapsible row.
  // Mixed auth statuses are handled by groupRowsHtml with a MIXED badge + breakdown.
  const ipMap = new Map();
  for (const e of rows) {
    if (!ipMap.has(e.ip)) ipMap.set(e.ip, []);
    ipMap.get(e.ip).push(e);
  }
  const allRuns = [];
  for (const entries of ipMap.values()) {
    allRuns.push(entries);
  }
  allRuns.sort((a, b) => b[0].ts > a[0].ts ? 1 : b[0].ts < a[0].ts ? -1 : 0);
  let html = '';
  for (const run of allRuns) {
    html += run.length >= 2 ? groupRowsHtml(run) : rowToHtml(run[0]);
  }

  document.getElementById('tbody').innerHTML =
    html || '<tr class="empty-row"><td colspan="7">No matching entries.</td></tr>';

  // Restore previously expanded groups by matching IP
  if (expandedIPs.size > 0) {
    document.querySelectorAll('.grp-summary[data-gid]').forEach(row => {
      if (expandedIPs.has(row.dataset.grpIp)) toggleGroup(parseInt(row.dataset.gid));
    });
  }

  document.getElementById('f-count').textContent =
    rows.length !== allRows.length ? `${rows.length} of ${allRows.length}` : `${allRows.length} entries`;
}

document.querySelectorAll('.f-btn[data-auth]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const val = btn.dataset.auth;
    const deactivate = filterAuth === val && val !== 'all';
    filterAuth = deactivate ? 'all' : val;
    document.querySelectorAll('.f-btn[data-auth]').forEach(b=>b.classList.remove('active'));
    if (!deactivate) btn.classList.add('active');
    else document.querySelector('.f-btn[data-auth="all"]').classList.add('active');
    applyFilters();
  });
});
document.querySelectorAll('.f-btn[data-origin]').forEach(btn => {
  btn.addEventListener('click', () => {
    const deactivate = btn.classList.contains('active');
    document.querySelectorAll('.f-btn[data-origin]').forEach(b => b.classList.remove('active'));
    filterOrigin = deactivate ? 'all' : btn.dataset.origin;
    if (!deactivate) btn.classList.add('active');
    applyFilters();
  });
});
document.getElementById('f-ip').addEventListener('input', e=>{
  filterIP = e.target.value.trim();
  applyFilters();
});
let countdown = 30;
let lastTime  = '';

function load(){
  fetch('/access/data').then(r=>r.json()).then(d=>{
    document.getElementById('s-total').textContent      = d.total;
    document.getElementById('s-ips').textContent        = d.unique_ips;
    document.getElementById('s-blocked').textContent    = d.blocked;
    document.getElementById('s-authed').textContent     = d.authed;
    document.getElementById('s-token').textContent      = d.token_visits;
    const hasThreats = d.blocked > 0;
    document.body.style.setProperty('--header-accent', hasThreats?'var(--red)':'var(--green)');
    if (d.server_date) today = d.server_date;
    lastTime  = new Date().toLocaleTimeString();
    countdown = 30;
    allRows   = d.log;
    applyFilters();
  }).catch(err=>{ lastTime='error'; console.error('[access] fetch /access/data failed:', err); });
}

function tick(){
  countdown = Math.max(0, countdown-1);
  document.getElementById('last-updated').textContent =
    lastTime ? `${lastTime}  \u2022  ${countdown}s` : '';
}

// ── Tab switching ──
function switchTab(tab) {
  if (!document.getElementById('panel-' + tab)) return;
  activeTab = tab;
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b => {
    const active = b.dataset.tab === tab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  ['dash'].forEach(t => {
    const panel = document.getElementById('panel-' + t);
    panel.classList.toggle('hidden', t !== tab);
  });
}

let activeTab = 'dash';
const _urlTab = new URLSearchParams(location.search).get('tab');
if (_urlTab) switchTab(_urlTab);

document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});


load();
setInterval(tick, 1000);
setInterval(load, 30000);
