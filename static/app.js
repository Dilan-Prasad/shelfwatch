/* Product Pulse — frontend. Vanilla JS, no build step.
   Screens: intake → generating (SSE-driven) → report (data-driven from the report JSON, see ../CONTRACT.md). */
'use strict';
(() => {
// ============================================================ helpers
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = esc;
const money = v => (v == null || isNaN(v)) ? '—' : '$' + Number(v).toFixed(2);
const num = n => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dparts = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || '')); return m ? { y: +m[1], m: +m[2], d: +m[3] } : null; };
const fmtDate = s => { const p = dparts(s); return p ? `${MONTHS[p.m - 1]} ${p.d}` : (s ? esc(s) : '—'); };
const fmtDateY = s => { const p = dparts(s); return p ? `${MONTHS[p.m - 1]} ${p.d}, ${p.y}` : (s ? esc(s) : '—'); };
const fmtMonthY = s => { const p = dparts(s); return p ? `${MONTHS[p.m - 1]} ${p.y}` : (s ? esc(s) : '—'); };
const fmtAsOf = iso => { if (!iso) return '—'; const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(iso); return m ? `${m[1]} ${m[2]} UTC` : esc(String(iso).slice(0, 16)); };
const addDays = (s, n) => { const p = dparts(s); if (!p) return null; return new Date(Date.UTC(p.y, p.m - 1, p.d + n)).toISOString().slice(0, 10); };
const sign = v => v > 0 ? '+' : v < 0 ? '−' : '';
const safeUrl = u => { const s = String(u || '').trim(); return /^https?:\/\//i.test(s) ? s : ''; };
const link = (u, text, cls, title) => { const s = safeUrl(u); const c = cls ? ` class="${cls}"` : ''; return s ? `<a href="${attr(s)}" target="_blank" rel="noopener"${c}>${text}</a>` : `<span${c} title="${attr(title || 'no source URL available')}">${text}</span>`; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const STATUS = { act: { color: '#FF5A4E', text: '#FF7A70', label: 'act now' }, watch: { color: '#F5B14A', text: '#F5B14A', label: 'watch' }, clear: { color: '#3DD68C', text: '#3DD68C', label: 'clear' }, thin: { color: '#6E7A94', text: '#6E7A94', label: 'thin data' } };
const TONE = { grey: '#A9B3C9', amber: '#F5B14A', red: '#FF7A70', green: '#3DD68C', blue: '#6F8BFF' };
const TREND = { rising: { dot: '#FF5A4E', text: '#FF7A70' }, flat: { dot: '#F5B14A', text: '#F5B14A' }, falling: { dot: '#3DD68C', text: '#3DD68C' }, new: { dot: '#6F8BFF', text: '#6F8BFF' } };
const SURFACES = [['reddit', 'reddit'], ['youtube', 'youtube'], ['tiktok', 'tiktok'], ['news', 'news'], ['forums', 'forums & blogs'], ['retail', 'retail reviews'], ['cpsc', 'cpsc']];
const SURFACE_LABEL = Object.fromEntries(SURFACES);
const SHARE_COLORS = ['#1840ED', '#6F8BFF', '#2A3A6B'];
const PALETTE = [250, 300, 215, 275, 330, 190, 45, 120, 20, 160].map(h => `oklch(74% 0.12 ${h})`);
const NOT_WALMART = 'Only walmart.com product URLs work in this demo.';
const WM_RE = /^(?:https?:\/\/)?(?:www\.|business\.)?walmart\.com\/(?:ip\/(?:[^\s?#]*?\/)?\d{5,}|reviews\/product\/\d{5,})(?:[?#][^\s]*)?$/i;
const STATIC = '/static/';

// ============================================================ state
const S = { view: 'intake', error: null, presets: [], url: '', gen: null, report: null, mock: false, es: null, shown: {}, hoverDay: null, deep: { status: 'idle' }, advanceTimer: null, chartModel: null };
let MOCK = null;
const newGen = () => ({ resolve: null, surfaces: SURFACES.map(([key, label]) => ({ key, label, status: 'queued', n: null, note: '' })), scanStarted: false, count: null, countDone: false, windowDays: 365, report: null, progress: 4, calls: [] });
const winLabel = d => (d >= 360 ? '12 months' : `${d || 90} days`);
const winShort = d => (d >= 360 ? '12mo' : `${d || 90}d`);

// ============================================================ intake view
function viewIntake() {
  const slots = [0, 1, 2].map(i => {
    const p = S.presets[i];
    if (!p) return `<span class="preset-slot">preset slot · team URL</span>`;
    return `<button class="preset" data-action="preset" data-url="${attr(p.url)}" title="${attr(p.url)}">${esc(p.label || p.url)}</button>${p.cached ? `<button class="preset-replay" data-action="replay" data-url="${attr(p.url)}" title="replay the cached report (no API calls)">↻ replay</button>` : ''}`;
  }).join('');
  return `
<div class="intake">
  <div class="topbar">
    <div class="brand"><span class="brand-dot"></span><span class="brand-name">Product Pulse</span><span class="brand-tag">demo</span></div>
    <div class="topbar-right">powered by Exa · external web only</div>
  </div>
  <div class="hero">
    <div class="eyebrow blue">Pulse report · one walmart.com product URL</div>
    <h1>Every product has a <span class="accent">pulse.</span></h1>
    <p class="lede">Paste a walmart.com product URL. Product Pulse reads the open web — Reddit, YouTube, TikTok, news, forums, CPSC — and returns one report: sentiment, price history, live listings, internal dupes, recall status.</p>
    <div class="urlbar">
      <input id="url" type="url" spellcheck="false" autocomplete="off" placeholder="https://www.walmart.com/ip/…" value="${attr(S.url)}">
      <button class="btn-primary" data-action="submit">Check the pulse →</button>
    </div>
    <div class="errline" id="errline">${S.error ? `<div class="err"><span class="dot"></span><span>${esc(S.error)}</span></div>` : ''}</div>
    <div class="presets"><span class="presets-label">demo presets</span>${slots}</div>
  </div>
  <div class="intake-foot">
    <span>sentiment pulse · historical price · listing radar · internal dupes · recall &amp; safety</span>
    <span>walmart.com excluded from sentiment</span>
  </div>
</div>`;
}

// ============================================================ generating view
const SURF_STYLE = {
  queued: { color: '#4F5B75', text: () => 'queued' },
  scanning: { color: '#6F8BFF', text: () => 'scanning', pulse: true },
  done: { color: '#EEF1F7', text: s => s.n != null ? `done · ${num(s.n)}` : 'done', check: true },
  thin: { color: '#F5B14A', text: s => s.n != null ? `thin · ${num(s.n)}` : 'thin' },
  indirect: { color: '#A9B3C9', text: s => s.n != null ? `indirect · ${num(s.n)}` : 'indirect' },
  degraded: { color: '#6E7A94', text: () => 'degraded' },
};
function resolveInner() {
  const r = S.gen && S.gen.resolve;
  if (!r) return `<div class="gen-wait"><span class="dot-pulse"></span>reading the walmart.com URL · asking Exa which product this is</div>`;
  const aliases = (r.aliases || []).slice(0, 4).map(a => `<span class="alias">“${esc(a.text)}”<b>${a.support != null ? Number(a.support).toFixed(2) : ''}</b></span>`).join('');
  return `
    <div class="gen-name">${esc(r.name || 'Unresolved product')}</div>
    <div class="gen-model">${esc(r.short || [r.brand, r.model].filter(Boolean).join(' / '))} · WMT:${esc(r.id || '')}</div>
    <div class="aliases">${aliases}</div>
    <div class="gen-resolved" style="animation-delay:${(0.9 + 0.5 * Math.min(3, (r.aliases || []).length)).toFixed(1)}s">✓ entity resolved · ${(r.aliases || []).length} web alias${(r.aliases || []).length === 1 ? '' : 'es'} matched</div>`;
}
function surfacesInner() {
  return S.gen.surfaces.map(s => {
    const st = SURF_STYLE[s.status] || SURF_STYLE.queued;
    return `<div class="surface-row"><span class="surface-name">${esc(s.label || SURFACE_LABEL[s.key] || s.key)}${s.note ? `<span class="surface-note">${esc(s.note)}</span>` : ''}</span><span class="surface-status" style="color:${st.color}">${st.pulse ? '<span class="dot-pulse"></span>' : ''}${st.check ? '<span>✓</span>' : ''}<span>${st.text(s)}</span></span></div>`;
  }).join('');
}
function viewGenerating() {
  const g = S.gen;
  return `
<div class="gen">
  <div class="progress" id="g-progress" style="width:${g.progress}%"></div>
  <div class="gen-top">
    <div class="brand"><span class="brand-dot pulse"></span><span class="brand-name">Product Pulse</span></div>
    <div class="topbar-right">generating · external web only</div>
  </div>
  <div class="gen-body" id="g-body">
    <div class="gen-step-head"><span class="eyebrow blue">01 · resolving entity</span><span class="gen-url" title="${attr(S.url)}">${esc(S.url)}</span></div>
    <div class="gen-card" id="g-resolve">${resolveInner()}</div>
    <div class="gen-scan" id="g-scan" ${g.scanStarted ? '' : 'hidden'}>
      <span class="eyebrow blue">02 · scanning surfaces</span>
      <div class="gen-list" id="g-surfaces">${surfacesInner()}</div>
    </div>
    <div class="gen-count" id="g-count" ${g.count != null ? '' : 'hidden'}>
      <div class="count-num" id="g-count-num">${g.countDone ? num(g.count) : '0'}</div>
      <div class="count-cap" id="g-count-cap">external mentions found in the last ${winLabel(g.windowDays)}.</div>
    </div>
  </div>
  <button class="skip" id="g-skip" data-action="skip" ${g.report ? '' : 'disabled'}>${g.report ? 'skip to report →' : 'building report…'}</button>
</div>`;
}
const patchProgress = () => { const el = $('#g-progress'); if (el && S.gen) el.style.width = S.gen.progress + '%'; };
const patchResolve = () => { const el = $('#g-resolve'); if (!el) return; const again = !!el.querySelector('.gen-name'); el.innerHTML = resolveInner(); if (again) el.classList.add('instant'); };
const patchSurfaces = () => { const sc = $('#g-scan'); if (sc) sc.hidden = false; const el = $('#g-surfaces'); if (el) el.innerHTML = surfacesInner(); };
const patchSkip = () => { const b = $('#g-skip'); if (!b) return; b.disabled = !(S.gen && S.gen.report); b.textContent = b.disabled ? 'building report…' : 'skip to report →'; };
function animateCount(target) {
  const el = $('#g-count-num'); const box = $('#g-count'); if (box) box.hidden = false; if (!el) return;
  const cap = $('#g-count-cap'); if (cap) cap.textContent = `external mentions found in the last ${winLabel(S.gen.windowDays)}.`;
  const t0 = performance.now(), dur = 1400;
  const step = now => {
    const p = Math.min(1, (now - t0) / dur);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString('en-US');
    if (p < 1) requestAnimationFrame(step); else { if (S.gen) { S.gen.countDone = true; maybeAdvance(); } }
  };
  requestAnimationFrame(step);
}

// ============================================================ report view
function viewReport(r) {
  return `
<div class="report">
  ${sticky(r)}
  ${secHeader(r)}
  ${secBoard(r)}
  ${secSentiment(r)}
  ${secPrice(r)}
  ${secListings(r)}
  ${secDupes(r)}
  ${secRecall(r)}
  ${footer(r)}
</div>`;
}
function sticky(r) {
  const p = r.product || {};
  return `
  <div class="sticky"><div class="sticky-in">
    <div class="brand"><span class="brand-dot small"></span><span class="brand-name s20">Product Pulse</span><span class="brand-sub">${esc(p.short || p.name || '')} · WMT:${esc(r.id || '')}</span></div>
    <div class="row8">
      <span class="pill-meta">as_of ${fmtAsOf(r.as_of)}</span>
      <span class="pill-meta">external web only</span>
      ${r.from_cache ? `<span class="pill-meta cached">cached · ${fmtAsOf(r.as_of)}</span>` : ''}
      <button class="btn-outline" data-action="reset">New report</button>
    </div>
  </div></div>`;
}
function secHeader(r) {
  const p = r.product || {}, v = r.verdict || {};
  const shortUrl = (() => { try { const u = new URL(safeUrl(r.url)); const id = r.id || u.pathname.split('/').filter(Boolean).pop(); return `walmart.com/ip/…/${id}`; } catch (e) { return 'walmart.com/ip/…'; } })();
  return `
  <section class="wrap rhead-sec rise">
    <div class="rhead">
      <div>
        <div class="eyebrow">Pulse report · ${esc(p.short || '')}</div>
        <h1>${esc(p.name || 'Unresolved product')}</h1>
        ${link(r.url, esc(shortUrl) + ' ↗', 'wm-link')}
      </div>
      <div class="pills-col">
        <span class="pill-meta">as_of ${fmtAsOf(r.as_of)}</span>
        <span class="pill-meta">external web only</span>
        <span class="pill-meta">walmart.com excluded from sentiment</span>
      </div>
    </div>
    <p class="verdict">${esc(v.lead || '')}${v.em || v.accent ? ` <em>${esc(v.em || '')}${v.accent ? ` <span class="accent ${esc(v.accent_tone || '')}">${esc(v.accent)}</span>` : ''}</em>` : ''}</p>
  </section>`;
}
function secBoard(r) {
  const b = (r.board || []).slice(0, 5);
  const counts = { act: 0, watch: 0, clear: 0, thin: 0 };
  b.forEach(c => { counts[STATUS[c.status] ? c.status : 'thin']++; });
  const legend = [['act', 'act now'], ['watch', 'watch'], ['clear', 'clear'], ['thin', 'thin data']].filter(([k]) => k !== 'thin' || counts.thin > 0)
    .map(([k, l]) => `<span><span class="dot s7" style="background:${STATUS[k].color}"></span>${counts[k]} ${l}</span>`).join('');
  const ids = ['s01', 's02', 's03', 's04', 's05'];
  const cards = b.map((c, i) => {
    const st = STATUS[c.status] || STATUS.thin;
    return `<a href="#${ids[i]}" class="bcard" style="border-top-color:${st.color}">
      <div class="bcard-top"><span class="bstatus" style="color:${st.text}"><span class="dot" style="background:${st.color};box-shadow:0 0 10px ${st.color}cc"></span>${st.label}</span><span class="bnum">${esc(c.num || ('0' + (i + 1)))} ↓</span></div>
      <div class="btitle">${esc(c.title || '')}</div>
      <div class="blines"><span style="color:${TONE[c.line1_color] || TONE.grey}">${esc(c.line1 || '')}</span><br><span style="color:${TONE[c.line2_color] || TONE.grey}">${esc(c.line2 || '')}</span></div></a>`;
  }).join('');
  return `
  <section class="wrap board-sec rise d1">
    <div class="board-head"><span class="eyebrow">Signal board · five verticals · what needs attention now</span><span class="legend">${legend}</span></div>
    <div class="board">${cards}</div>
  </section>`;
}
function secHead(num, title, question, metaHtml) {
  return `<div class="sec-head"><div><div class="eyebrow">${num} · ${title}</div><h2>${question}</h2></div><div class="sec-meta">${metaHtml}</div></div>`;
}
function thinCard(label, title, sub) { return `<div class="thin-card"><span class="eyebrow">${esc(label)}</span><div class="t">${esc(title)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ''}</div>`; }

// ---------- 01 sentiment
function sparkSvg(spark) {
  if (!Array.isArray(spark)) return null;
  const vals = spark.filter(v => v != null && !isNaN(v)); if (vals.length < 2) return null;
  const lo = Math.min(...vals) - 0.02, hi = Math.max(...vals) + 0.02; const n = spark.length;
  const sy = v => (40 - (v - lo) / (hi - lo) * 36).toFixed(1);
  let d = '', pen = false, lastX = 178, lastV = vals[vals.length - 1];
  spark.forEach((v, i) => { if (v == null || isNaN(v)) { pen = false; return; } const px = (2 + i / (n - 1) * 176).toFixed(1); d += (pen ? 'L' : 'M') + px + ' ' + sy(v); pen = true; lastX = px; lastV = v; });
  return `<svg viewBox="0 0 182 44" width="182" height="44" class="spark"><path d="${d}" fill="none" stroke="#EEF1F7" stroke-width="1.5" stroke-linejoin="round"></path><circle cx="${lastX}" cy="${sy(lastV)}" r="3" fill="#EEF1F7"></circle></svg>`;
}
function trendText(c) {
  const t = c.trend || 'flat', pct = c.trend_pct;
  if (t === 'rising') return `RISING${pct != null ? ` +${Math.round(pct)}%` : ''}`;
  if (t === 'falling') return `FALLING${pct != null ? ` −${Math.round(Math.abs(pct))}%` : ''}`;
  if (t === 'new') return 'NEW';
  return 'FLAT';
}
function evidenceList(c, rank) {
  const ev = c.evidence || [];
  if (!ev.length) return `<div class="evidence" id="ev-${rank}" hidden><span class="dim mono" style="font-size:12px">no evidence links were attached to this cluster</span></div>`;
  return `<div class="evidence" id="ev-${rank}" hidden><ul>${ev.map(e => `<li>${link(e.url, esc(e.title || e.url || 'source'))}<span class="ev-meta">${esc(SURFACE_LABEL[e.source] || e.source || '')}${e.date ? ' · ' + fmtDate(e.date) : ''}</span></li>`).join('')}</ul></div>`;
}
function secSentiment(r) {
  const s = r.sentiment || {}, m = r.mentions || {}, clusters = s.clusters || [], praises = s.praises || [];
  const asof = fmtAsOf(r.as_of);
  const head = secHead('01', 'Sentiment Pulse', 'How do people actually feel about this product?', `as_of ${asof}<br>external web only · walmart.com excluded`);
  const d = s.delta30;
  const dchip = d == null ? `<span class="chip chip-grey">n/a · 30d</span>` : `<span class="chip ${d < -0.005 ? 'chip-amber' : d > 0.005 ? 'chip-green' : 'chip-grey'}">${sign(d)}${Math.abs(d).toFixed(2)} · 30d</span>`;
  const v = m.velocity_pct;
  const vchip = v == null ? `<span class="chip chip-grey">n/a</span>` : `<span class="chip chip-grey">${v > 0 ? '↑' : v < 0 ? '↓' : '→'}${Math.abs(Math.round(v))}% vs prior 30d</span>`;
  const rising = clusters.filter(c => c.trend === 'rising').length;
  const spark = sparkSvg(s.spark);
  const retail = s.retail && s.retail.rating != null ? `<div class="stat-sub">${link(s.retail.url, `${esc(s.retail.merchant || 'retail')} ${Number(s.retail.rating).toFixed(1)}★ · ${num(s.retail.review_count)} reviews ↗`)}</div>` : '';
  const stats = `
    <div class="stats">
      <div class="stat"><div class="stat-label">sentiment score</div><div class="stat-val"><span class="stat-num">${s.score != null ? Number(s.score).toFixed(2) : '—'}</span>${dchip}</div><div class="stat-sub">${esc(s.score != null ? (s.trend_word || '') : `insufficient signal · ${num(s.n_labeled || 0)} labeled`)}</div>${retail}</div>
      <div class="stat"><div class="stat-label">mentions · ${winShort(m.window_days)}</div><div class="stat-val"><span class="stat-num">${num(m.total)}</span>${vchip}</div><div class="stat-sub">last 30d · ${num(m.last30)} · prior 30d · ${num(m.prev30)}</div></div>
      <div class="stat"><div class="stat-label">sentiment · 90d</div>${spark || `<div class="spark-cap" style="margin-top:14px">not enough dated mentions for a trend line</div>`}<div class="spark-cap">${s.score_prev != null && s.score_last30 != null && s.delta30 != null ? `${Number(s.score_prev).toFixed(2)} → ${Number(s.score_last30).toFixed(2)} over 30d` : (s.n_labeled != null ? `${num(s.n_labeled)} labeled mentions` : '')}</div></div>
      <div class="stat"><div class="stat-label">open pain clusters</div><div class="stat-val"><span class="stat-num">${clusters.length}</span>${rising ? `<span class="chip chip-red">${rising} rising</span>` : `<span class="chip chip-grey">none rising</span>`}</div><div class="stat-sub">${clusters.length ? 'ranked below' : 'no recurring complaint found'}</div></div>
    </div>`;
  let body = '';
  if (!clusters.length) {
    body = `<article class="cluster empty"><div><div class="cl-rank"><span class="dot" style="background:#3DD68C"></span><span class="cl-label" style="color:#3DD68C">no pain cluster</span></div><h3 class="cl-title">No recurring complaint surfaced across ${num(s.n_labeled || 0)} labeled external mentions.</h3><div class="cl-meta"><span>${num(m.total)} mentions retrieved</span><span class="sep">·</span><span>complaints are only counted when a reviewer or owner voices them</span></div></div></article>`;
  } else {
    const c = clusters[0], tr = TREND[c.trend] || TREND.flat;
    const srcs = (c.sources || []).slice(0, 3);
    const bar = srcs.length ? `<div class="share"><div class="share-bar">${srcs.map((x, i) => `<span style="flex:${Math.max(1, Math.round(x.pct || 0))};background:${SHARE_COLORS[i]}"></span>`).join('')}</div><div class="share-legend">${srcs.map((x, i) => `<span><i style="background:${SHARE_COLORS[i]}"></i>${esc(SURFACE_LABEL[x.key] || x.key)} ${Math.round(x.pct || 0)}%</span>`).join('')}</div></div>` : '';
    const quote = c.quote && c.quote.text ? `<blockquote><p>“${esc(c.quote.text)}”</p>${link(c.quote.url, esc(c.quote.source_label || 'source') + ' ↗', 'cl-src')}</blockquote>` : `<blockquote><p class="dim" style="font-size:20px">No verbatim quote passed the attribution check for this cluster.</p></blockquote>`;
    const n = c.mentions != null ? c.mentions : (c.evidence || []).length;
    body = `
    <article class="cluster">
      <div>
        <div class="cl-rank"><span class="dot" style="background:${tr.dot}"></span><span class="cl-label" style="color:${tr.text}">01 · ${esc(c.trend || 'flat')}</span></div>
        <h3 class="cl-title">${esc(c.title || 'Unlabeled complaint')}</h3>
        <div class="cl-meta"><span>${num(n)} mention${n === 1 ? '' : 's'}</span><span class="sep">·</span><span style="color:${tr.text}">${trendText(c)}</span>${c.first_seen ? `<span class="sep">·</span><span>first seen ${fmtDate(c.first_seen)}</span>` : ''}</div>
        ${bar}
      </div>
      <div class="cl-right">${quote}<div class="cl-actions"><span class="viewall" data-action="viewall" data-rank="1" data-label="View all ${num(n)} ↗">View all ${num(n)} ↗</span></div></div>
    </article>${evidenceList(c, 1)}`;
    body += clusters.slice(1).map((c2, i) => {
      const t2 = TREND[c2.trend] || TREND.flat, rank = i + 2, n2 = c2.mentions != null ? c2.mentions : (c2.evidence || []).length;
      return `<article class="cluster-row">
        <div class="cl-left"><div class="cl-rank w110"><span class="dot" style="background:${t2.dot}"></span><span class="cl-label" style="color:${t2.text}">${String(rank).padStart(2, '0')} · ${esc(c2.trend || 'flat')}</span></div><h3 class="cl-title-s">${esc(c2.title || 'Unlabeled complaint')}</h3></div>
        <div class="cl-row-right"><span>${num(n2)} mention${n2 === 1 ? '' : 's'} <span class="sep">·</span> <span style="color:${t2.text}">${trendText(c2)}</span></span><span class="viewall" data-action="viewall" data-rank="${rank}" data-label="View all ${num(n2)} ↗">View all ${num(n2)} ↗</span></div>
      </article>${evidenceList(c2, rank)}`;
    }).join('');
  }
  const working = `<div class="working"><span class="lab">what's working</span>${praises.length ? praises.slice(0, 3).map(p => `<span class="work"><span class="dot"></span>${esc(p.title)} <span class="n">${num(p.mentions)} · ${esc(p.trend || 'flat')}</span></span>`).join('') : `<span class="work dim" style="font-size:13px">no recurring praise extracted from the labeled mentions</span>`}</div>`;
  return `<section id="s01" class="wrap sec first rise d2">${head}${stats}${body}${working}</section>`;
}

// ---------- 02 price
const Chart = (() => {
  const X0 = 56, X1 = 1050, Y0 = 20, Y1 = 340;
  const nice = raw => { const steps = [1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000]; for (const s of steps) if (s >= raw) return s; return 20000; };
  const fmtTick = v => Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, '');
  function model(price) {
    const ser = price.series || {}; const N = Math.max(2, ser.days || 90); const start = ser.start_date || null;
    const merchants = (ser.merchants || []).map((m, i) => {
      const pts = (m.points || []).filter(p => p && p.price != null && p.day != null && !isNaN(p.price)).map(p => ({ day: clamp(Math.round(p.day), 0, N - 1), price: +p.price, url: p.url })).sort((a, b) => a.day - b.day);
      const vals = new Array(N).fill(null); let cur = null, pi = 0;
      for (let d = 0; d < N; d++) { while (pi < pts.length && pts[pi].day <= d) { cur = pts[pi].price; pi++; } vals[d] = cur; }
      return { key: m.key || ('m' + i), name: m.name || m.key || ('merchant ' + (i + 1)), color: m.color || PALETTE[i % PALETTE.length], long_tail: !!m.long_tail, points: pts, vals, first: pts.length ? pts[0].day : null, oos: m.oos_from_day != null ? clamp(Math.round(m.oos_from_day), 0, N - 1) : null };
    }).filter(m => m.points.length);
    const arr = a => Array.isArray(a) && a.length ? Array.from({ length: N }, (_, d) => (a[d] == null || isNaN(a[d])) ? null : +a[d]) : null;
    let low = arr(ser.low), high = arr(ser.high), median = arr(ser.median);
    if (!low || !high || !median) {
      const cl = [], ch = [], cm = [];
      for (let d = 0; d < N; d++) { const vs = merchants.map(m => m.vals[d]).filter(v => v != null).sort((a, b) => a - b); if (!vs.length) { cl.push(null); ch.push(null); cm.push(null); continue; } cl.push(vs[0]); ch.push(vs[vs.length - 1]); const mid = vs.length >> 1; cm.push(vs.length % 2 ? vs[mid] : (vs[mid - 1] + vs[mid]) / 2); }
      low = low || cl; high = high || ch; median = median || cm;
    }
    const wm = price.walmart && price.walmart.price != null && !isNaN(price.walmart.price) ? +price.walmart.price : null;
    const all = []; merchants.forEach(m => m.points.forEach(p => all.push(p.price))); [low, high, median].forEach(a => a.forEach(v => { if (v != null) all.push(v); })); if (wm != null) all.push(wm);
    let lo = Math.min(...all), hi = Math.max(...all); if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 100; } if (hi - lo < 1) { lo -= 10; hi += 10; }
    const pad = (hi - lo) * 0.12; const step = nice((hi - lo + 2 * pad) / 5);
    const ymin = Math.floor((lo - pad) / step) * step, ymax = Math.ceil((hi + pad) / step) * step;
    const x = d => X0 + d / (N - 1) * (X1 - X0); const y = p => Y1 - (p - ymin) / (ymax - ymin) * (Y1 - Y0);
    const ticks = []; for (let v = ymin; v <= ymax + 1e-9; v += step) ticks.push(+v.toFixed(2));
    const xl = [];
    if (start) { xl.push({ d: 0, label: fmtDate(start), anchor: 'start' }); for (let d = 1; d < N - 1; d++) { const s = addDays(start, d), p = dparts(s); if (p && p.d === 1 && d >= 7 && d <= N - 8) xl.push({ d, label: fmtDate(s), anchor: 'middle' }); } xl.push({ d: N - 1, label: fmtDate(addDays(start, N - 1)), anchor: 'end' }); }
    const events = (price.events || []).filter(e => e && e.day != null).map(e => ({ ...e, day: clamp(Math.round(e.day), 0, N - 1) }));
    const medianNow = price.median_now != null ? +price.median_now : (median.filter(v => v != null).slice(-1)[0] ?? null);
    return { N, start, merchants, low, high, median, wm, x, y, ymin, ymax, ticks, xl, events, medianNow, lowestDay: price.walmart_lowest_day != null ? clamp(Math.round(price.walmart_lowest_day), 0, N - 1) : null };
  }
  function stepPath(vals, from, to, x, y, N) {
    let s = '', pen = false;
    for (let d = from; d <= to; d++) { const v = vals[d]; if (v == null) { pen = false; continue; } const px = x(d).toFixed(1), py = y(v).toFixed(1); s += pen ? `H${px}V${py}` : `M${px} ${py}`; pen = true; }
    if (to < N - 1 && pen) s += `H${x(to + 1).toFixed(1)}`;
    return s;
  }
  function bandPath(low, high, x, y, N) {
    let f = -1; for (let d = 0; d < N; d++) if (low[d] != null && high[d] != null) { f = d; break; }
    if (f < 0) return '';
    let last = N - 1; while (last > f && high[last] == null) last--;
    let s = stepPath(low, f, N - 1, x, y, N);
    if (!s) return '';
    s += `L${x(last).toFixed(1)} ${y(high[last]).toFixed(1)}`;
    for (let d = last - 1; d >= f; d--) { if (high[d] == null) continue; s += `V${y(high[d]).toFixed(1)}H${x(d).toFixed(1)}`; }
    return s + 'Z';
  }
  function svg(m, shown) {
    const { x, y, N } = m;
    const grid = m.ticks.map((v, i) => `<line x1="${X0}" x2="${X1}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${i === 0 ? '#3A4560' : '#1B2437'}" stroke-width="1"></line>`).join('');
    const ylab = m.ticks.map(v => `<text x="44" y="${(y(v) + 4).toFixed(1)}">$${fmtTick(v)}</text>`).join('');
    const xlab = m.xl.map(l => `<text x="${x(l.d).toFixed(1)}" y="368" text-anchor="${l.anchor}">${esc(l.label)}</text>`).join('');
    const band = bandPath(m.low, m.high, x, y, N);
    const lines = m.merchants.map(mm => {
      if (!shown[mm.key]) return '';
      const end = mm.oos != null ? Math.max(mm.first, mm.oos - 1) : N - 1;
      let s = `<path d="${stepPath(mm.vals, mm.first, end, x, y, N)}" fill="none" stroke="${mm.color}" stroke-width="1.5"></path>`;
      if (mm.oos != null && mm.oos <= N - 1 && mm.oos > mm.first) s += `<path d="${stepPath(mm.vals, mm.oos, N - 1, x, y, N)}" fill="none" stroke="${mm.color}" stroke-width="1.5" stroke-dasharray="3 4"></path>`;
      return s;
    }).join('');
    let wmLayer = '';
    if (m.wm != null) {
      const wy = y(m.wm);
      wmLayer += `<line x1="${X0}" x2="${X1}" y1="${wy.toFixed(1)}" y2="${wy.toFixed(1)}" stroke="#EEF1F7" stroke-width="2.5"></line><text x="1064" y="${(wy + 4).toFixed(1)}" font-size="12" font-weight="500" fill="#EEF1F7">Walmart ${money(m.wm)}</text>`;
      if (m.medianNow != null) {
        let my = y(m.medianNow); if (Math.abs(my - wy) < 16) my = wy + (my >= wy ? 16 : -16);
        const diff = m.medianNow - m.wm, col = diff < 0 ? '#FF5A4E' : diff > 0 ? '#3DD68C' : '#A9B3C9';
        wmLayer += `<text x="1064" y="${(my + 4).toFixed(1)}" font-size="12" fill="#A9B3C9">web median ${money(m.medianNow)}</text>`;
        if (Math.abs(diff) >= 0.01) {
          const a = Math.min(wy, my) + 7, b = Math.max(wy, my) - 7;
          if (b - a > 8) wmLayer += `<line x1="1056" x2="1056" y1="${a.toFixed(1)}" y2="${b.toFixed(1)}" stroke="${col}" stroke-width="1"></line><line x1="1052" x2="1060" y1="${a.toFixed(1)}" y2="${a.toFixed(1)}" stroke="${col}" stroke-width="1"></line><line x1="1052" x2="1060" y1="${b.toFixed(1)}" y2="${b.toFixed(1)}" stroke="${col}" stroke-width="1"></line>`;
          const ty = b - a > 30 ? (a + b) / 2 + 4 : Math.max(wy, my) + 20;
          wmLayer += `<text x="1064" y="${ty.toFixed(1)}" font-size="12" font-weight="500" fill="${col}">${sign(diff)}${money(Math.abs(diff))}</text>`;
        }
      }
    } else if (m.medianNow != null) {
      wmLayer += `<text x="1064" y="${(y(m.medianNow) + 4).toFixed(1)}" font-size="12" fill="#A9B3C9">web median ${money(m.medianNow)}</text>`;
    }
    let lowest = '';
    if (m.lowestDay != null && m.wm != null) {
      const lx = x(m.lowestDay).toFixed(1);
      lowest = `<line x1="${lx}" x2="${lx}" y1="${y(m.wm).toFixed(1)}" y2="${Y1}" stroke="#6E7A94" stroke-width="1" stroke-dasharray="2 3"></line><text x="${(+lx + 7).toFixed(1)}" y="${(y(m.wm) + 21).toFixed(1)}" font-size="11" fill="#6E7A94">${esc(fmtDate(addDays(m.start, m.lowestDay)))} · last day Walmart was the lowest web price</text>`;
    }
    const sorted = [...m.events].sort((a, b) => a.day - b.day);
    const ev = sorted.map((e, i) => {
      const yy = m.median[e.day] ?? m.low[e.day]; const cy = yy != null ? y(yy) : Y1;
      const dp = i > 0 ? e.day - sorted[i - 1].day : Infinity, dn = i < sorted.length - 1 ? sorted[i + 1].day - e.day : Infinity;
      const right = (dn < 8 && dn <= dp) || (e.day > N * 0.92 && dp >= 11);   // true = label sits on the LEFT of the marker
      return `<g transform="translate(${x(e.day).toFixed(1)} 0)"><line x1="0" x2="0" y1="27" y2="${cy.toFixed(1)}" stroke="#EEF1F7" stroke-opacity=".3" stroke-dasharray="2 3"></line><circle cx="0" cy="${cy.toFixed(1)}" r="4.5" fill="#070A12" stroke="#EEF1F7" stroke-width="1.5"></circle><circle cx="0" cy="18" r="9" fill="#1840ED"></circle><text x="0" y="21.5" text-anchor="middle" fill="#FFFFFF" font-size="10.5" font-weight="500">${esc(e.n ?? '')}</text><text x="${right ? -14 : 14}" y="22"${right ? ' text-anchor="end"' : ''} fill="#A9B3C9" font-size="12">${esc(fmtDate(e.date))}</text></g>`;
    }).join('');
    const hoverCircles = m.merchants.map(mm => `<circle id="ch-m-${attr(mm.key)}" cx="0" cy="0" r="3.5" fill="${mm.color}" stroke="#070A12" stroke-width="1.5" opacity="0"></circle>`).join('');
    return `<svg viewBox="0 0 1200 400" width="100%" xmlns="http://www.w3.org/2000/svg">
      <g stroke-width="1">${grid}</g>
      <g fill="#6E7A94" font-size="12" text-anchor="end">${ylab}</g>
      <g fill="#6E7A94" font-size="12">${xlab}</g>
      ${band ? `<path d="${band}" fill="#1840ED" fill-opacity="0.16"></path>` : ''}
      <path d="${stepPath(m.low, 0, N - 1, x, y, N)}" fill="none" stroke="#6F8BFF" stroke-opacity=".45" stroke-width="1"></path>
      <path d="${stepPath(m.high, 0, N - 1, x, y, N)}" fill="none" stroke="#6F8BFF" stroke-opacity=".45" stroke-width="1"></path>
      ${lowest}
      ${lines}
      <path d="${stepPath(m.median, 0, N - 1, x, y, N)}" fill="none" stroke="#6F8BFF" stroke-width="1.75" stroke-dasharray="6 4"></path>
      ${wmLayer}
      ${ev}
      <line id="ch-x" x1="0" x2="0" y1="20" y2="340" stroke="#EEF1F7" stroke-opacity=".45" stroke-dasharray="2 3" opacity="0"></line>
      ${hoverCircles}
      <circle id="ch-med" cx="0" cy="0" r="4" fill="#070A12" stroke="#6F8BFF" stroke-width="1.75" opacity="0"></circle>
      <circle id="ch-wm" cx="0" cy="0" r="4.5" fill="#EEF1F7" opacity="0"></circle>
      <rect id="ch-overlay" x="${X0}" y="20" width="${X1 - X0}" height="320" fill="transparent" style="cursor:crosshair"></rect>
    </svg>`;
  }
  function tooltip(m, shown, day) {
    const rows = m.merchants.map(mm => { const p = mm.vals[day]; return { name: mm.name, color: mm.color, price: p, note: (mm.oos != null && day >= mm.oos) ? 'OOS' : (mm.first === day ? 'NEW' : ''), weight: 400, dim: (p == null || !shown[mm.key]) ? .45 : 1 }; });
    if (m.wm != null) rows.push({ name: 'Walmart', color: '#EEF1F7', price: m.wm, note: '', weight: 600, dim: 1 });
    rows.sort((a, b) => (a.price ?? 1e9) - (b.price ?? 1e9));
    const ev = m.events.find(e => e.day === day);
    const date = m.start ? addDays(m.start, day) : null; const p = dparts(date);
    return `<div class="tt-head"><span class="tt-date">${p ? `${MONTHS[p.m - 1]} ${p.d}` : `day ${day}`}</span><span class="tt-year">${p ? p.y : ''}</span></div>
      <div class="tt-range"><span>low ${money(m.low[day])}</span><span>med ${money(m.median[day])}</span><span>high ${money(m.high[day])}</span></div>
      ${rows.map(r => `<div class="tt-row" style="opacity:${r.dim};font-weight:${r.weight}"><span class="l"><i style="background:${r.color}"></i>${esc(r.name)}</span><span class="r">${r.note ? `<span class="note">${r.note}</span>` : ''}<span>${money(r.price)}</span></span></div>`).join('')}
      ${ev ? `<div class="tt-ev"><b>event · </b>${esc(ev.label || '')}</div>` : ''}`;
  }
  function hoverUpdate(container, m, shown, day) {
    const on = day != null; const xline = $('#ch-x', container); const tip = $('#chart-tip', container);
    if (!xline) return;
    const hx = on ? m.x(day).toFixed(1) : 0;
    xline.setAttribute('x1', hx); xline.setAttribute('x2', hx); xline.setAttribute('opacity', on ? 1 : 0);
    m.merchants.forEach(mm => { const c = $('#ch-m-' + CSS.escape(mm.key), container); if (!c) return; const p = on ? mm.vals[day] : null; const vis = on && shown[mm.key] && p != null; c.setAttribute('cx', hx); c.setAttribute('cy', p != null ? m.y(p).toFixed(1) : 0); c.setAttribute('opacity', vis ? 1 : 0); });
    const med = $('#ch-med', container); const mv = on ? m.median[day] : null; med.setAttribute('cx', hx); med.setAttribute('cy', mv != null ? m.y(mv).toFixed(1) : 0); med.setAttribute('opacity', on && mv != null ? 1 : 0);
    const wmc = $('#ch-wm', container); wmc.setAttribute('cx', hx); wmc.setAttribute('cy', m.wm != null ? m.y(m.wm).toFixed(1) : 0); wmc.setAttribute('opacity', on && m.wm != null ? 1 : 0);
    if (tip) { if (!on) tip.hidden = true; else { tip.hidden = false; tip.innerHTML = tooltip(m, shown, day); tip.style.left = (m.x(day) / 1200 * 100).toFixed(2) + '%'; tip.style.transform = day > m.N * 0.58 ? 'translateX(calc(-100% - 14px))' : 'translateX(14px)'; } }
  }
  function mount(container, price, shown) {
    const m = model(price);
    container.innerHTML = `<div id="chart-svg">${svg(m, shown)}</div><div class="tooltip" id="chart-tip" hidden></div>`;
    const ov = $('#ch-overlay', container);
    if (ov) {
      ov.addEventListener('mousemove', e => { const r = ov.getBoundingClientRect(); const day = clamp(Math.round((e.clientX - r.left) / r.width * (m.N - 1)), 0, m.N - 1); if (day !== S.hoverDay) { S.hoverDay = day; hoverUpdate(container, m, shown, day); } });
      ov.addEventListener('mouseleave', () => { S.hoverDay = null; hoverUpdate(container, m, shown, null); });
    }
    return m;
  }
  return { model, mount };
})();
function priceTone(text) { const t = String(text || '').toLowerCase(); if (/down|drop|fell|lower|cheaper|cut/.test(t)) return ''; if (/\bup\b|rose|higher|pricier|increase/.test(t)) return 'green'; return 'blue'; }
function secPrice(r) {
  const P = r.price || {}; const asof = fmtAsOf(r.as_of); const days = (P.series && P.series.days) || 90;
  const head = secHead('02', 'The Price Question', 'Is this product getting more expensive or cheaper across the web?', `as_of ${asof}<br>${winLabel(days)} · exact-product listings`);
  const hasSeries = P.series && Array.isArray(P.series.merchants) && P.series.merchants.some(m => m && m.points && m.points.length);
  if (!hasSeries) {
    const h = P.headline || {};
    return `<section id="s02" class="wrap sec rise d3">${head}${thinCard('thin data', h.lead ? `${h.lead}${h.accent || ''}${h.tail || ''}` : 'No dated price observations for this exact product were found on the open web in the last 90 days.', P.method_note || 'Price history needs dated, exact-product price observations — deal posts, merchant listings, price trackers. None were retrievable for this item.')}</section>`;
  }
  const h = P.headline || {};
  const headline = h.lead || h.accent ? `<p class="price-head">${esc(h.lead || '')}<span class="accent ${priceTone(h.accent)}">${esc(h.accent || '')}</span>${esc(h.tail || '')}${h.em ? ` <em>${esc(h.em)}</em>` : ''}</p>` : '';
  const m = Chart.model(P);
  const toggles = m.merchants.map(mm => `<button class="mtoggle" data-action="toggle" data-key="${attr(mm.key)}"><i style="border-color:${mm.color};background:${S.shown[mm.key] ? mm.color : 'transparent'}"></i><span style="color:${S.shown[mm.key] ? '#EEF1F7' : '#6E7A94'}">${esc(mm.name)}</span></button>`).join('');
  const legend = `<div class="chart-legend" id="chart-legend"><span class="lg"><span class="lg-band"></span>web low–high</span><span class="lg"><span class="lg-med"></span>web median</span>${P.walmart ? `<span class="lg"><span class="lg-wm"></span>Walmart</span>` : ''}<span class="lg-sep"></span><span class="lg-lab">merchants</span>${toggles}</div>`;
  const events = (P.events || []).slice(0, 4);
  const evHtml = events.length ? `<div class="events">${events.map(e => `<div class="ev"><span class="ev-n">${esc(e.n ?? '')}</span><div class="ev-body"><span class="ev-date">${fmtDate(e.date)}</span><br>${esc(e.label || '')} ${link(e.url, 'source ↗', 'ev-src')}</div></div>`).join('')}</div>` : `<div class="under">no dated price event was found inside the ${winLabel(days)} window</div>`;
  const rng = P.range || null; const wm = P.walmart;
  const diff = wm && wm.price != null && P.median_now != null ? P.median_now - wm.price : null;
  const strip = `<div class="pstrip">
      <div><span class="k">${winShort(days)} range</span><span class="v">${rng ? `${money(rng.low)}–${money(rng.high)}` : '—'}</span></div>
      <div><span class="k">Walmart position</span><span class="v">${esc(P.walmart_position || (wm ? '—' : 'unknown (walmart price not observed)'))}</span></div>
      <div><span class="k">web median</span><span class="v">${money(P.median_now)}</span>${wm && wm.price != null ? `<span class="k" style="margin-left:10px">vs Walmart</span><span class="v">${money(wm.price)}</span>${diff != null ? `<span class="v ${diff < 0 ? 'neg' : diff > 0 ? 'pos' : ''}">(${sign(diff)}${money(Math.abs(diff))})</span>` : ''}` : `<span class="k" style="margin-left:10px">Walmart</span><span class="v dim">not observed</span>`}</div>
    </div>`;
  const ah = P.amazon_history;
  const ahHtml = ah && (ah.lowest != null || ah.current != null) ? `<div class="pstrip ah"><div><span class="k">${esc(ah.label || 'Amazon price history')}</span>${ah.current != null ? `<span class="k" style="margin-left:10px">current</span><span class="v">${money(ah.current)}</span>` : ''}${ah.lowest != null ? `<span class="k" style="margin-left:10px">all-time low</span><span class="v">${money(ah.lowest)}</span>` : ''}${ah.highest != null ? `<span class="k" style="margin-left:10px">high</span><span class="v">${money(ah.highest)}</span>` : ''}${ah.average != null ? `<span class="k" style="margin-left:10px">average</span><span class="v">${money(ah.average)}</span>` : ''}<span class="k" style="margin-left:10px">${link(ah.url, esc(ah.host || 'source') + ' ↗')}</span></div></div>` : '';
  const wmNote = wm && wm.price != null ? `<div class="under">Walmart ${money(wm.price)} = last price observed on the open web${wm.observed ? ` on ${fmtDateY(wm.observed)}` : ''}${wm.source_label ? ` via ${link(wm.url, esc(wm.source_label) + ' ↗')}` : ''}; walmart.com itself blocks crawlers.</div>` : '';
  return `<section id="s02" class="wrap sec rise d3">${head}${headline}${legend}<div class="chart-wrap" id="price-chart"></div>${evHtml}${strip}${ahHtml}<p class="method"><b>Method</b> — ${esc(P.method_note || 'exact-product listings only (accessories and variants excluded); each merchant line carries its last observed price forward.')}</p>${wmNote}</section>`;
}

// ---------- 03 listings
function listingsHeadline(L) {
  const prices = (L.rows || []).filter(x => x.price != null).map(x => +x.price);
  if (!prices.length) return { lead: 'No priced listing found on the open web.', accent: '' };
  if (L.walmart_price == null) return { lead: 'Walmart price not observed on the open web.', accent: '' };
  const low = Math.min(...prices);
  if (L.walmart_price <= low + 0.005) return { lead: "Walmart's last observed price is ", accent: 'the web low.' };
  return { lead: 'Walmart is ', accent: 'not the lowest price.' };
}
function radarTone(text) { const t = String(text || '').toLowerCase(); if (/not the lowest|above|higher/.test(t)) return ''; if (/web low|lowest|cheapest/.test(t)) return 'green'; return 'grey'; }
function secListings(r, instant) {
  const L = r.listings || {}; const rows = L.rows || []; const cls = instant ? 'instant' : '';
  const head = secHead('03', 'Listing Radar', 'Who is selling this exact product right now — and where does Walmart stand?', `live ${esc(L.live_date || String(r.as_of || '').slice(0, 10))}<br>${rows.length} merchant${rows.length === 1 ? '' : 's'} · exact listings`);
  const deep = L.deep_scan && L.deep_scan.available ? `<div class="deep"><span class="deep-note">${esc(L.deep_scan.note || 'Exa Agent + Affiliate.com catalog')}</span><button class="btn-outline" id="deep-btn" data-action="deepscan" ${S.deep.status === 'running' ? 'disabled' : ''}>${S.deep.status === 'running' ? 'Deep scan running · Exa Agent + Affiliate.com…' : S.deep.status === 'done' ? 'Deep scan merged ✓' : S.deep.status === 'error' ? 'deep scan unavailable' : 'Deep scan · Exa Agent + Affiliate.com'}</button></div>` : '';
  if (!rows.length) return `<section id="s03" class="wrap sec rise d4 ${cls}">${head}${thinCard('thin data', 'No live listing for this exact product was found on the open web.', 'Exa searched the major retailers and the long tail for exact-product listings; nothing that passed the exact-match check was returned.')}${deep}</section>`;
  const prices = rows.filter(x => x.price != null).map(x => +x.price);
  const wmp = L.walmart_price != null ? +L.walmart_price : null;
  const range = L.range && L.range.low != null ? L.range : (prices.length ? { low: Math.min(...prices), high: Math.max(...prices) } : null);
  const h = L.headline || listingsHeadline(L);
  const barLow = range ? Math.min(range.low, wmp ?? range.low) : 0, barHigh = range ? Math.max(range.high, wmp ?? range.high) : 1;
  const pct = p => barHigh > barLow ? clamp((p - barLow) / (barHigh - barLow) * 100, 0, 100) : 50;
  let ticks = Array.isArray(L.ticks) && L.ticks.length ? L.ticks : (() => { const by = {}; rows.filter(x => x.price != null).forEach(x => { (by[+x.price] = by[+x.price] || []).push(x.merchant); }); return Object.keys(by).map(Number).sort((a, b) => a - b).map(p => ({ price: p, labels: by[p] })); })();
  if (ticks.length > 5) { const t = ticks; ticks = [...new Map([t[0], t[Math.round((t.length - 1) / 4)], t[Math.round((t.length - 1) / 2)], t[Math.round(3 * (t.length - 1) / 4)], t[t.length - 1]].map(x => [x.price, x])).values()]; }
  // cluster ticks that sit within 6% of each other on the bar, then lay labels out on two rows if they would collide
  const groups = [];
  ticks.slice().sort((a, b) => +a.price - +b.price).forEach(t => { const g = groups[groups.length - 1]; if (g && pct(+t.price) - pct(g.hi) < 6) { g.hi = +t.price; g.labels.push(...(t.labels || [])); } else groups.push({ lo: +t.price, hi: +t.price, labels: [...(t.labels || [])] }); });
  const BAR_PX = 1000, CH = 7.2; let prevRight = [-1e9, -1e9];
  const tickHtml = groups.map(g => {
    const p = pct((g.lo + g.hi) / 2); const text = (g.lo === g.hi ? money(g.lo) : `${money(g.lo)}–${money(g.hi)}`); const lab = g.labels.join(' · ');
    const w = (text.length + 1 + lab.length) * CH; const cx = p / 100 * BAR_PX;
    let left = p <= 3 ? cx : p >= 97 ? cx - w : cx - w / 2; left = clamp(left, 0, BAR_PX - w);
    const row = left <= prevRight[0] + 10 ? (left <= prevRight[1] + 10 ? 0 : 1) : 0; prevRight[row] = left + w;
    const style = `left:${(left / BAR_PX * 100).toFixed(2)}%;top:${row ? 64 : 46}px`;
    return `<div class="tick" style="left:${p.toFixed(2)}%"></div>${g.lo !== g.hi ? `<div class="tick" style="left:${pct(g.lo).toFixed(2)}%"></div><div class="tick" style="left:${pct(g.hi).toFixed(2)}%"></div>` : ''}<div class="tlab" style="${style}">${text} <span class="m">${esc(lab)}</span></div>`;
  }).join('');
  let wmHtml = '';
  if (wmp != null) { const p = pct(wmp); const green = prices.length && wmp <= Math.min(...prices) + 0.005; wmHtml = `<div class="wm-dot ${green ? 'green' : ''}" style="left:${p.toFixed(2)}%"></div><div class="wm-lab ${green ? 'green' : ''}" style="${p > 50 ? `left:${p.toFixed(2)}%;transform:translateX(-100%)` : `left:${p.toFixed(2)}%`}">Walmart ${money(wmp)}</div>`; }
  const radar = `<div class="radar">
      <div class="radar-head"><div class="radar-title">${esc(h.lead || '')}<span class="accent ${radarTone(h.accent)}">${esc(h.accent || '')}</span></div><div class="radar-range">web range ${range ? `${money(range.low)}–${money(range.high)}` : '—'} · Walmart ${wmp != null ? money(wmp) : 'not observed'}</div></div>
      <div class="pbar"><div class="track"></div><div class="grad"></div>${tickHtml}${wmHtml}</div>
    </div>`;
  const chips = (L.chips || []).length ? `<div class="chips">${L.chips.map(c => `<span class="sig ${esc(c.tone || 'grey')}"><span class="dot"></span>${esc(c.text || c.kind || '')}</span>`).join('')}</div>` : '';
  const lowPrice = prices.length ? Math.min(...prices) : null;
  const trs = rows.map(x => {
    const d = x.delta30 != null ? +x.delta30 : null;
    const dcell = x.delta30_note ? `<td class="r"><span class="chip chip-amber">${esc(x.delta30_note)}</span></td>` : d == null ? `<td class="r dim">—</td>` : d < -0.005 ? `<td class="r drop">▼ −${Math.abs(d).toFixed(2)}</td>` : d > 0.005 ? `<td class="r rise">▲ +${d.toFixed(2)}</td>` : `<td class="r dim">flat</td>`;
    const stock = x.stock === 'OOS' ? `<span class="chip chip-amber">OOS</span>` : (!x.stock || x.stock === 'unknown') ? `<span class="dim">unknown</span>` : esc(x.stock);
    const name = `<span class="mname">${esc(x.merchant || 'unknown merchant')}${x.long_tail ? '<span class="lt">long tail</span>' : ''}${x.deep ? '<span class="lt" style="border-color:rgba(111,139,255,.5);color:#6F8BFF">deep scan</span>' : ''}${x.seller ? `<span class="mseller">${esc(x.seller)}</span>` : ''}</span>`;
    const isLow = x.price != null && lowPrice != null && +x.price <= lowPrice + 0.005;
    const pnote = x.price_note ? `<div class="pnote">${x.price_url ? link(x.price_url, esc(x.price_note) + ' ↗', 'pnote-link') : esc(x.price_note)}</div>` : '';
    return `<tr><td>${name}</td><td class="r price${isLow || (d != null && d < 0) ? ' b' : ''}">${x.price != null ? Number(x.price).toFixed(2) : '—'}${pnote}</td>${dcell}<td>${stock}</td><td>${esc(x.type || 'exact')}</td><td class="${x.first_seen ? '' : 'dim'}">${x.first_seen ? esc(x.first_seen) : '—'}</td><td class="r">${link(x.url, 'open ↗', 'open')}</td></tr>`;
  }).join('');
  const table = `<table class="tbl"><thead><tr><th>merchant</th><th class="r">price</th><th class="r">Δ 30d</th><th>stock</th><th>listing type</th><th>first seen</th><th class="r">open</th></tr></thead><tbody>${trs}</tbody></table>`;
  const lt = rows.filter(x => x.long_tail).length;
  const note = `<p class="method"><b>Long tail</b> — ${lt ? `${lt} merchant${lt === 1 ? '' : 's'} outside the big-box set. Competitors no in-house price monitor covers; found by searching the open web for the exact product.` : 'no long-tail merchant surfaced for this product; only big-box listings were found.'}</p>`;
  return `<section id="s03" class="wrap sec rise d4 ${cls}">${head}${radar}${chips}${table}${note}${deep}</section>`;
}

// ---------- 04 dupes
function secDupes(r, instant) {
  const D = r.dupes || {}; const cls = instant ? 'instant' : ''; const exact = D.exact || []; const other = D.other || []; const n = D.count_exact != null ? D.count_exact : exact.length;
  const P = r.price || {}, L = r.listings || {};
  const head = secHead('04', 'Internal Dupes', 'Is Walmart selling this product against itself?', `as_of ${fmtAsOf(r.as_of)}<br>walmart.com listings only`);
  const pill = n > 0 ? `<span class="sig amber"><span class="dot"></span>${n} active walmart.com listing${n > 1 ? 's' : ''} for this exact product</span>` : `<span class="sig green"><span class="dot"></span>no duplicate walmart.com listing found for this exact product</span>`;
  const prim = D.primary || { id: r.id, url: r.url, title: (r.product || {}).name };
  const primPrice = prim.price != null ? +prim.price : (L.walmart_price != null ? +L.walmart_price : (P.walmart && P.walmart.price != null ? +P.walmart.price : null));
  const primPriceNote = prim.price == null && primPrice != null ? `<span class="dimv sans">last observed on the open web</span>` : '';
  let grid = '';
  if (n > 0) {
    const shown = exact.slice(0, 3); const letters = ['B', 'C', 'D'];
    const cols = `grid-template-columns:150px repeat(${shown.length + 1},minmax(0,1fr))`;
    const cell = (content, i, cls) => `<div class="${cls || ''}${i > 0 ? ' b' : ''}">${content}</div>`;
    const row = (label, cells) => `<div class="k">${label}</div>${cells.map((c, i) => cell(c.html, i, c.cls)).join('')}`;
    const itemCells = [{ html: `${link(prim.url, `WMT:${esc(prim.id || r.id)} ↗`)}${prim.title ? `<div class="dtitle">${esc(prim.title)}</div>` : ''}` }].concat(shown.map(d => ({ html: `${link(d.url, `WMT:${esc(d.id)} ↗`)}${d.title ? `<div class="dtitle">${esc(d.title)}</div>` : ''}` })));
    const priceCells = [{ html: `<div class="cell"><span class="big">${primPrice != null ? money(primPrice) : '—'}</span>${primPriceNote}</div>` }].concat(shown.map(d => { let chip = ''; if (d.price != null && primPrice != null) { const diff = +d.price - primPrice; chip = Math.abs(diff) < 0.005 ? `<span class="chip chip-grey">same price</span>` : diff < 0 ? `<span class="chip chip-red">${money(Math.abs(diff))} under primary</span>` : `<span class="chip chip-grey">${money(diff)} over primary</span>`; } return { html: `<div class="cell"><span class="big${d.price != null ? ' bold' : ''}">${d.price != null ? money(d.price) : '—'}</span>${chip}${d.price == null ? `<span class="dimv sans">not crawlable</span>` : ''}</div>` }; }));
    const rv = o => o && o.rating != null ? `${Number(o.rating).toFixed(1)}★ <span class="dim">·</span> ${num(o.reviews)}` : `<span class="dimv">—</span>`;
    const reviewCells = [{ html: rv(prim) }].concat(shown.map(d => ({ html: rv(d) })));
    const srcCells = [{ html: `<span class="sans">${esc(prim.seller || '—')}</span>` }].concat(shown.map(d => ({ html: `<span class="sans">${esc(d.seller || '—')}</span>` })));
    const seenCells = [{ html: `<span class="dimv">${prim.indexed ? fmtMonthY(prim.indexed) : '—'}</span>` }].concat(shown.map(d => ({ html: d.indexed ? `<span style="font-size:14px">${fmtMonthY(d.indexed)}</span>` : `<span class="dimv">—</span>` })));
    grid = `<div class="dgrid" style="${cols}">
      <div class="h k"></div><div class="h">Listing A · primary</div>${shown.map((d, i) => `<div class="h b">Listing ${letters[i]} · ${esc(d.seller || 'walmart.com')}</div>`).join('')}
      ${row('item', itemCells)}${row('price', priceCells)}${row('reviews', reviewCells)}${row('source', srcCells)}${row('first seen', seenCells)}
    </div>${D.note ? `<div class="under">${esc(D.note)}</div>` : ''}`;
  } else {
    grid = `<div class="dupe-clear"><div class="cl-rank"><span class="dot" style="background:#3DD68C"></span><span class="cl-label" style="color:#3DD68C">clear</span></div><div class="t">Only one walmart.com listing carries this exact product in Exa's index of walmart.com.</div>${D.note ? `<div class="under">${esc(D.note)}</div>` : ''}</div>`;
  }
  const rest = exact.slice(3).concat(other);
  const others = rest.length ? `<div class="others"><div class="lab">other walmart.com listings in the same family · ${rest.length}</div><ul>${rest.map(d => `<li>${link(d.url, esc(d.title || ('WMT:' + d.id)))}<span class="m"><span class="lt">${esc(d.kind || 'related')}</span>WMT:${esc(d.id || '')}${d.indexed ? ` · ${fmtMonthY(d.indexed)}` : ''}</span></li>`).join('')}</ul></div>` : '';
  const summary = D.summary || (n > 0 ? `${n} additional walmart.com listing${n > 1 ? 's carry' : ' carries'} this exact product. A shopper searching the product can land on a page other than the primary — review equity splits across pages, and ad spend can end up bidding against itself.` : 'No sibling listing found for this exact product, so review equity and ad spend are not being split across walmart.com pages.');
  const foot = `<div class="dupe-foot"><p>${esc(summary)}</p><div style="display:flex;justify-content:flex-end"><span class="suggest"><span class="lab">suggested</span>${esc(D.suggestion || (n > 0 ? 'Consolidate listings or align pricing' : 'No action'))}</span></div></div>`;
  return `<section id="s04" class="wrap sec rise d5 ${cls}">${head}<div style="margin-top:32px">${pill}</div>${grid}${others}${foot}</section>`;
}

// ---------- 05 recall
function secRecall(r) {
  const R = r.recall || {}; const ml = R.model_level || {}; const fam = R.brand_family || []; const cs = R.complaint_scan || { count: 0, items: [] };
  const brand = (r.product || {}).brand || 'the brand', model = (r.product || {}).model || 'this model';
  const head = secHead('05', 'Recall &amp; Safety', 'Is anything actually unsafe?', `verified ${esc(R.verified || String(r.as_of || '').slice(0, 10))}<br>cpsc.gov + news surfaces`);
  const st = ml.status === 'act' ? { color: '#FF5A4E', text: '#FF7A70', label: 'model-level · recall found' } : ml.status === 'thin' ? { color: '#6E7A94', text: '#6E7A94', label: 'model-level · thin data' } : { color: '#3DD68C', text: '#3DD68C', label: 'model-level · clear' };
  const items = (ml.items || []).length ? `<div class="ritems">${ml.items.map(it => `<div class="ritem"><b>${esc(it.product || 'Recall')}</b>${it.date ? ` — ${fmtDateY(it.date)}` : ''}${it.units ? ` · ${esc(it.units)}` : ''}${it.hazard ? ` · ${esc(it.hazard)}` : ''} ${link(it.url, 'cpsc.gov ↗')}</div>`).join('')}</div>` : '';
  const modelCard = `<div class="rcard ${ml.status === 'act' ? 'act' : ''}">
      <div><div class="cl-rank"><span class="dot" style="background:${st.color};box-shadow:0 0 0 4px ${st.color}24"></span><span class="cl-label" style="color:${st.text}">${st.label}</span></div><h3>${esc(ml.headline || `No CPSC recall found for the ${model}.`)}</h3>${items}</div>
      <div class="rcard-foot"><span>verified ${fmtDateY(R.verified || String(r.as_of || '').slice(0, 10))} · cpsc.gov recall database + news surfaces</span><a href="https://www.cpsc.gov/Recalls" target="_blank" rel="noopener">cpsc.gov ↗</a></div>
    </div>`;
  const famCard = fam.length ? `<div class="rfam">
      <div class="rfam-head"><div class="cl-rank"><span class="dot" style="background:#F5B14A"></span><span class="cl-label" style="color:#F5B14A">brand-family spillover · watch</span></div><span class="exa-pill">Exa-unique insight</span></div>
      ${fam.slice(0, 3).map(f => `<p class="p1"><b>${esc(f.product || '')}</b>${f.date ? ` — recalled ${fmtDateY(f.date)}` : ''}${f.units ? `, ${esc(f.units)}` : ''}${f.hazard ? `, ${esc(f.hazard)}` : ''}. <em>${esc(f.why || 'A sibling product, not this model.')}</em></p>`).join('')}
      <p class="p2">Why it's on the page: customers Googling “${esc(brand)} recall” find it; brand-trust spillover is a real search behavior.</p>
      ${link(fam[0].url, 'source · cpsc.gov ↗', 'src')}
    </div>` : `<div class="rfam none">
      <div class="rfam-head"><div class="cl-rank"><span class="dot" style="background:#3DD68C"></span><span class="cl-label" style="color:#3DD68C">brand family · clear</span></div><span class="exa-pill" style="color:#6E7A94;border-color:#1B2437">Exa-unique insight</span></div>
      <p class="p1">No other ${esc(brand)} product carries a CPSC recall notice in the last 24 months.</p>
      <p class="p2">Checked because customers Googling “${esc(brand)} recall” would find one; brand-trust spillover is a real search behavior.</p>
      <a class="src" href="https://www.cpsc.gov/Recalls" target="_blank" rel="noopener">source · cpsc.gov ↗</a>
    </div>`;
  const scan = cs.count > 0 ? `<div class="cscan"><span class="lab">complaint scan</span><span><span style="color:#F5B14A">${cs.count} safety-classified complaint${cs.count === 1 ? '' : 's'}</span> in the product's mention graph this quarter <span class="dim">(safety claims are tracked separately from quality complaints).</span></span><ul>${(cs.items || []).slice(0, 5).map(it => `<li>${link(it.url, esc(it.title || it.url || 'source'))}${it.date ? ` <span class="dim mono" style="font-size:11px">${fmtDate(it.date)}</span>` : ''}</li>`).join('')}</ul></div>`
    : `<div class="cscan"><span class="lab">complaint scan</span><span>No safety-classified complaint in the product's mention graph this quarter <span class="dim">(safety claims are tracked separately from quality complaints).</span></span></div>`;
  const sn = R.safety_news || [];
  const KIND = { recall: 'recall', lawsuit: 'lawsuit', incident_report: 'incident', regulatory: 'regulatory', investigation: 'investigation' };
  const newsBlock = sn.length ? `<div class="snews"><div class="snews-head"><span class="lab">safety news · ${esc(brand)} · 12 months</span><span class="dim mono" style="font-size:11px">news + agency surfaces · brand-level, not this model unless marked</span></div><ul>${sn.map(x => `<li><span class="kind ${esc(x.kind || '')}">${esc(KIND[x.kind] || x.kind || 'news')}</span>${link(x.url, esc(x.title || x.product || 'source'))}${x.issue ? `<span class="issue">${esc(x.issue)}</span>` : ''}<span class="ev-meta">${esc(x.host || '')}${x.date ? ' · ' + fmtDateY(x.date) : ''}${x.applies_to_model ? ' · <b style="color:#FF7A70">names this model</b>' : ''}</span></li>`).join('')}</ul></div>` : '';
  return `<section id="s05" class="wrap sec rise d6">${head}<div class="rgrid">${modelCard}${famCard}</div>${scan}${newsBlock}</section>`;
}
function footer(r) {
  const c = r.cost || {};
  return `<footer class="rfoot rise d7"><span>Product Pulse · demo · collected via Exa${c.calls != null ? ` · ${num(c.calls)} API calls` : ''}${c.dollars != null ? ` · $${Number(c.dollars).toFixed(2)}` : ''}</span><span>as_of ${fmtAsOf(r.as_of)} · external web only · walmart.com excluded from sentiment</span></footer>`;
}
function mountReport(r) {
  const P = r.price;
  if (P && P.series && Array.isArray(P.series.merchants)) {
    initShown(r);
    const c = $('#price-chart'); if (c) S.chartModel = Chart.mount(c, P, S.shown);
  }
}
function toggleMerchant(key) {
  S.shown[key] = !S.shown[key];
  const r = S.report; if (!r || !r.price) return;
  const c = $('#price-chart'); if (c) S.chartModel = Chart.mount(c, r.price, S.shown);
  $$('.mtoggle').forEach(b => { const k = b.dataset.key, on = !!S.shown[k]; const i = b.querySelector('i'), sp = b.querySelector('span'); if (i) i.style.background = on ? i.style.borderColor : 'transparent'; if (sp) sp.style.color = on ? '#EEF1F7' : '#6E7A94'; });
}

// ============================================================ deep scan (opt-in)
function mergeDeep(r, j) {
  const L = r.listings = r.listings || { rows: [] }; L.rows = L.rows || [];
  const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  let walmart = j.walmart || null;
  (j.offers || []).forEach(o => {
    if (!o || o.price_usd == null || isNaN(o.price_usd)) return;
    const m = String(o.merchant || '').trim() || 'unknown merchant';
    if (/walmart/i.test(m)) { if (!walmart) walmart = { price: +o.price_usd, url: o.url }; return; }
    const row = L.rows.find(x => norm(x.merchant) === norm(m) || (norm(x.merchant).length > 3 && norm(m).includes(norm(x.merchant))));
    if (row) { row.price = +o.price_usd; if (o.in_stock != null) row.stock = o.in_stock ? 'in stock' : 'OOS'; if (safeUrl(o.url)) row.url = o.url; row.deep = true; }
    else L.rows.push({ merchant: m, price: +o.price_usd, delta30: null, delta30_note: null, stock: o.in_stock == null ? 'unknown' : (o.in_stock ? 'in stock' : 'OOS'), type: 'exact', first_seen: null, url: o.url || '', long_tail: true, seller: o.condition && !/new/i.test(o.condition) ? o.condition : null, deep: true });
  });
  if (walmart && walmart.price != null) { L.walmart_price = +walmart.price; L.walmart_url = walmart.url; if (r.dupes && r.dupes.primary) { r.dupes.primary.price = +walmart.price; } }
  const prices = L.rows.filter(x => x.price != null).map(x => +x.price);
  if (prices.length) { L.range = { low: Math.min(...prices), high: Math.max(...prices) }; L.ticks = null; }
  L.headline = listingsHeadline(L); L.n_merchants = L.rows.length; L.deep_scan = { ...(L.deep_scan || {}), status: 'done' };
}
function rerenderListings() {
  const r = S.report; if (!r) return;
  const a = $('#s03'), b = $('#s04');
  if (a) a.outerHTML = secListings(r, true);
  if (b) b.outerHTML = secDupes(r, true);
}
async function deepScan() {
  const r = S.report; if (!r || S.deep.status === 'running') return;
  S.deep = { status: 'running' }; rerenderListings();
  const fail = msg => { S.deep = { status: 'error', message: msg }; rerenderListings(); };
  if (S.mock) { setTimeout(() => { mergeDeep(r, { status: 'done', offers: [{ merchant: 'Kohl\'s', price_usd: 119.99, in_stock: true, url: 'https://www.kohls.com/product/prd-3397105/ninja-air-fryer.jsp', condition: 'new' }, { merchant: 'Home Depot', price_usd: 90, in_stock: true, url: 'https://www.homedepot.com/p/307348719', condition: 'new' }, { merchant: 'Target', price_usd: 99.99, in_stock: true, url: 'https://www.target.com/p/ninja-4qt-air-fryer-black-af101/-/A-53649826', condition: 'new' }], walmart: { price: 119.99, url: r.url } }); S.deep = { status: 'done' }; rerenderListings(); }, 2500); return; }
  try {
    const res = await fetch(`/api/deepscan?id=${encodeURIComponent(r.id)}`, { method: 'POST' });
    if (!res.ok) { let msg = 'unavailable'; try { msg = (await res.json()).message || msg; } catch (e) { /* ignore */ } return fail(msg); }
    const poll = async () => {
      if (S.report !== r) return;
      try {
        const p = await fetch(`/api/deepscan?id=${encodeURIComponent(r.id)}`); if (!p.ok) return fail('unavailable');
        const j = await p.json();
        if (j.status === 'done') { mergeDeep(r, j); S.deep = { status: 'done' }; rerenderListings(); }
        else if (j.status === 'error') fail(j.message || 'error');
        else setTimeout(poll, 5000);
      } catch (e) { fail('unavailable'); }
    };
    setTimeout(poll, 4000);
  } catch (e) { fail('unavailable'); }
}

// ============================================================ flow
function render() {
  const app = $('#app');
  if (S.view === 'intake') app.innerHTML = viewIntake();
  else if (S.view === 'generating') app.innerHTML = viewGenerating();
  else { initShown(S.report || {}); app.innerHTML = viewReport(S.report || {}); mountReport(S.report || {}); }
}
function initShown(r) {
  const P = r.price;
  if (P && P.series && Array.isArray(P.series.merchants) && !Object.keys(S.shown).length) P.series.merchants.forEach(m => { if (m && m.key) S.shown[m.key] = true; });
}
function showError(msg) { S.error = msg; const el = $('#errline'); if (el) el.innerHTML = msg ? `<div class="err"><span class="dot"></span><span>${esc(msg)}</span></div>` : ''; }
function submit() {
  const inp = $('#url'); const v = ((inp && inp.value) || '').trim(); S.url = v;
  if (!WM_RE.test(v)) { showError(NOT_WALMART); return; }
  startRun(v, 'live');
}
function startRun(url, mode) {
  if (S.es) { try { S.es.close(); } catch (e) { /* ignore */ } S.es = null; }
  if (S.advanceTimer) { clearTimeout(S.advanceTimer); S.advanceTimer = null; }
  S.url = url; S.view = 'generating'; S.gen = newGen(); S.error = null; S.report = null; S.shown = {}; S.hoverDay = null; S.deep = { status: 'idle' };
  render(); window.scrollTo(0, 0);
  if (S.mock) { simulate(); return; }
  const es = new EventSource(`/api/pulse?url=${encodeURIComponent(url)}&mode=${encodeURIComponent(mode || 'live')}`);
  S.es = es;
  const parse = e => { try { return JSON.parse(e.data); } catch (err) { return null; } };
  es.addEventListener('resolve', e => { const d = parse(e); if (d) onResolve(d); });
  es.addEventListener('surface', e => { const d = parse(e); if (d) onSurface(d); });
  es.addEventListener('count', e => { const d = parse(e); if (d) onCount(d); });
  es.addEventListener('call', e => { const d = parse(e); if (d && S.gen) S.gen.calls.push(d); });
  es.addEventListener('report', e => { const d = parse(e); es.close(); S.es = null; if (d) onReport(d); else fail({ message: 'The report could not be read. Try again.', code: 'upstream' }); });
  es.addEventListener('error', e => {
    if (e && typeof e.data === 'string') { const d = parse(e) || { message: 'The pulse service returned an error.' }; es.close(); S.es = null; fail(d); return; }
    if (S.gen && S.gen.report) return;                      // stream ended after the report — nothing to do
    if (es.readyState === EventSource.CONNECTING) return;    // transient reconnect; the server will resume or end the stream
    es.close(); S.es = null; fail({ message: 'Connection to the pulse service was lost. Try again.', code: 'upstream' });
  });
}
function fail(d) {
  if (S.advanceTimer) { clearTimeout(S.advanceTimer); S.advanceTimer = null; }
  S.gen = null; S.view = 'intake';
  S.error = (d && d.code === 'not_walmart') ? NOT_WALMART : ((d && d.message) || 'Something went wrong. Try again.');
  render(); window.scrollTo(0, 0);
}
function onResolve(d) { const g = S.gen; if (!g) return; g.resolve = d; g.progress = Math.max(g.progress, 25); patchResolve(); patchProgress(); }
function onSurface(d) {
  const g = S.gen; if (!g || !d.key) return;
  let s = g.surfaces.find(x => x.key === d.key);
  if (!s) { s = { key: d.key, label: d.label || d.key, status: 'queued', n: null, note: '' }; g.surfaces.push(s); }
  if (d.status) s.status = d.status; if (d.n != null) s.n = d.n; if (d.note != null) s.note = d.note; if (d.label) s.label = d.label;
  g.scanStarted = true;
  const finished = g.surfaces.filter(x => ['done', 'thin', 'indirect', 'degraded'].includes(x.status)).length;
  g.progress = Math.max(g.progress, 25 + 55 * finished / Math.max(1, g.surfaces.length));
  patchSurfaces(); patchProgress();
}
function onCount(d) { const g = S.gen; if (!g) return; g.count = d.mentions != null ? +d.mentions : 0; if (d.window_days) g.windowDays = d.window_days; g.progress = Math.max(g.progress, 90); patchProgress(); animateCount(g.count); }
function onReport(r) {
  const g = S.gen; if (!g) return;
  g.report = r; S.report = r; g.progress = 100; patchProgress(); patchSkip();
  if (g.count == null) { g.countDone = true; }                 // no count event: advance without the animation
  maybeAdvance();
}
function maybeAdvance() { const g = S.gen; if (!g || !g.report || !g.countDone || S.advanceTimer) return; S.advanceTimer = setTimeout(() => { S.advanceTimer = null; if (S.view === 'generating') showReport(); }, 1500); }
function showReport() {
  if (S.advanceTimer) { clearTimeout(S.advanceTimer); S.advanceTimer = null; }
  if (!S.report) return;
  S.view = 'report'; S.hoverDay = null; render(); window.scrollTo(0, 0);
  if (!S.mock) { try { history.replaceState(null, '', `?url=${encodeURIComponent(S.report.url || S.url)}&auto=cached`); } catch (e) { /* ignore */ } }
}
function reset() {
  if (S.es) { try { S.es.close(); } catch (e) { /* ignore */ } S.es = null; }
  if (S.advanceTimer) { clearTimeout(S.advanceTimer); S.advanceTimer = null; }
  S.view = 'intake'; S.error = null; S.report = null; S.gen = null; S.url = ''; S.deep = { status: 'idle' };
  render(); window.scrollTo(0, 0);
  if (!S.mock) { try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ } }
}
async function loadPresets() {
  if (S.mock) { S.presets = MOCK ? [{ label: 'Ninja AF101 Air Fryer · 4 qt', url: MOCK.url, cached: true }] : []; if (S.view === 'intake') render(); return; }
  try { const res = await fetch('/api/presets'); if (!res.ok) return; const j = await res.json(); if (Array.isArray(j)) { S.presets = j.filter(p => p && p.url).slice(0, 3); if (S.view === 'intake') render(); } } catch (e) { /* no backend: leave slots empty */ }
}
function simulate() {
  const M = MOCK; if (!M) { fail({ message: 'mock.json could not be loaded.' }); return; }
  const t = (ms, fn) => setTimeout(() => { if (S.view === 'generating' && S.gen) fn(); }, ms);
  t(700, () => onResolve({ id: M.id, url: M.url, name: M.product.name, brand: M.product.brand, model: M.product.model, short: M.product.short, aliases: M.product.aliases }));
  const base = 3600, step = 550;
  (M.surfaces || []).forEach((s, i) => { t(base + i * step, () => onSurface({ key: s.key, label: s.label, status: 'scanning' })); t(base + i * step + step - 30, () => onSurface(s)); });
  const end = base + (M.surfaces || []).length * step + 300;
  t(end, () => onCount({ mentions: M.mentions.total, window_days: M.mentions.window_days }));
  t(end + 400, () => onReport({ ...M, from_cache: false }));
}

// ============================================================ events
document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]'); if (!el) return;
  const a = el.dataset.action;
  if (a === 'submit') submit();
  else if (a === 'preset') { const inp = $('#url'); if (inp) { inp.value = el.dataset.url; inp.focus(); } S.url = el.dataset.url; showError(null); }
  else if (a === 'replay') startRun(el.dataset.url, 'cached');
  else if (a === 'reset') reset();
  else if (a === 'skip') { if (S.gen && S.gen.report) showReport(); }
  else if (a === 'toggle') toggleMerchant(el.dataset.key);
  else if (a === 'viewall') { const ev = $('#ev-' + el.dataset.rank); if (ev) { ev.hidden = !ev.hidden; el.textContent = ev.hidden ? el.dataset.label : 'hide ↑'; } }
  else if (a === 'deepscan') deepScan();
});
document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target && e.target.id === 'url') { e.preventDefault(); submit(); } });
document.addEventListener('input', e => { if (e.target && e.target.id === 'url' && S.error) showError(null); });

// ============================================================ init
async function init() {
  const q = new URLSearchParams(location.search);
  S.mock = q.get('mock') === '1';
  if (S.mock) { try { MOCK = await (await fetch(STATIC + 'mock.json')).json(); } catch (e) { MOCK = null; } }
  if (S.mock && q.get('view') === 'report') { if (MOCK) { S.report = MOCK; S.view = 'report'; render(); return; } }
  const u = (q.get('url') || '').trim();
  if (u && WM_RE.test(u)) { startRun(u, q.get('auto') === 'cached' ? 'cached' : 'live'); loadPresets(); return; }
  if (u && !WM_RE.test(u)) { S.url = u; S.error = NOT_WALMART; }
  render(); loadPresets();
}
init();
})();
