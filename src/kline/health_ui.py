"""Small browser-visible source health and provenance surface."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/health-ui", response_class=HTMLResponse, include_in_schema=False)
async def health_ui() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Datafeed Source Health</title>
  <style>
    :root { color-scheme: dark; --bg:#091019; --panel:#101a26; --line:#253447;
      --text:#e8f0f7; --muted:#8ea2b7; --ok:#39d98a; --bad:#ff6b6b; --accent:#58a6ff; }
    * { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
      font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif; }
    main { max-width:1180px; margin:0 auto; padding:32px 24px 48px; }
    header { display:flex; justify-content:space-between; gap:24px; align-items:end; margin-bottom:24px; }
    h1 { font-size:27px; margin:0 0 6px } p { margin:0; color:var(--muted) }
    .stamp { text-align:right; color:var(--muted) } .grid { display:grid;
      grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; margin-bottom:28px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
    .row { display:flex; justify-content:space-between; gap:12px; align-items:center; }
    .source { font-weight:700; font-size:15px } .pill { border-radius:999px; padding:3px 8px;
      font-size:12px; background:#1b2938; color:var(--muted) }
    .pill.ok { color:var(--ok); background:#0e2b22 } .pill.bad { color:var(--bad); background:#341b20 }
    dl { display:grid; grid-template-columns:1fr auto; gap:7px 12px; margin:14px 0 0 }
    dt { color:var(--muted) } dd { margin:0; text-align:right; max-width:190px; overflow:hidden;
      text-overflow:ellipsis; white-space:nowrap }
    h2 { font-size:17px; margin:0 0 12px } table { width:100%; border-collapse:collapse;
      background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    th,td { padding:11px 12px; border-bottom:1px solid var(--line); text-align:left }
    th { color:var(--muted); font-size:12px; font-weight:600 } tr:last-child td { border:0 }
    .empty { color:var(--muted); padding:20px } .error { color:var(--bad) }
  </style>
</head>
<body><main>
  <header><div><h1>Datafeed Source Health</h1><p>Availability, provenance and source-scoped storage coverage.</p></div>
    <div class="stamp" id="stamp">Loading…</div></header>
  <div class="grid" id="sources"></div>
  <h2>Stored source coverage</h2>
  <div id="coverage"></div>
  <h2 style="margin-top:28px">GOLD 5m source comparison</h2>
  <div id="comparison" class="card empty">Waiting for two stored GOLD sources…</div>
</main><script>
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
fetch('/api/health').then(r => r.json()).then(data => {
  document.getElementById('stamp').textContent = `${data.service} ${data.version} · ${new Date().toLocaleString()}`;
  const observations = Object.fromEntries((data.latest_observations || []).map(x => [x.source_id, x]));
  const sources = data.providers?.sources || {};
  document.getElementById('sources').innerHTML = Object.entries(sources).map(([id, src]) => {
    const obs = observations[id]; const state = !src.available ? 'unavailable' : !obs ? 'registered' : obs.success ? 'healthy' : 'failed';
    const klass = state === 'healthy' ? 'ok' : state === 'failed' || state === 'unavailable' ? 'bad' : '';
    return `<section class="card"><div class="row"><span class="source">${esc(id)}</span><span class="pill ${klass}">${esc(state)}</span></div>
      <dl><dt>Provider</dt><dd>${esc(src.provider)}</dd><dt>Asset / market</dt><dd>${esc(src.asset_class)} · ${esc(src.market_type)}</dd>
      <dt>Execution venue</dt><dd>${src.execution_venue ? 'yes' : 'no'}</dd><dt>Realtime</dt><dd>${src.realtime_supported ? 'yes' : 'no'}</dd>
      <dt>Last instrument</dt><dd>${esc(obs?.ticker)}</dd><dt>Latest candle</dt><dd>${esc(obs?.latest_timestamp)}</dd>
      <dt>Latency</dt><dd>${obs?.latency_ms == null ? '—' : Math.round(obs.latency_ms) + ' ms'}</dd><dt>Last error</dt><dd title="${esc(obs?.error)}">${esc(obs?.error)}</dd></dl></section>`;
  }).join('');
  const rows = data.storage_coverage || [];
  document.getElementById('coverage').innerHTML = rows.length ? `<table><thead><tr><th>Source</th><th>Asset</th><th>Symbol</th><th>TF</th><th>Bars</th><th>Latest</th></tr></thead><tbody>${rows.map(x => `<tr><td>${esc(x.source_id)}</td><td>${esc(x.asset_class)}</td><td>${esc(x.ticker)}</td><td>${esc(x.timeframe)}</td><td>${esc(x.count)}</td><td>${esc(x.latest_timestamp)}</td></tr>`).join('')}</tbody></table>` : '<div class="card empty">No source-scoped candles stored yet.</div>';
  const ids = new Set(rows.filter(x => x.asset_class === 'commodity' && x.timeframe === '5m').map(x => x.source_id));
  if (ids.has('binance_usdm_futures') && ids.has('yahoo_finance_futures')) {
    fetch('/api/compare/commodity/GOLD?timeframe=5m&sources=binance_usdm_futures&sources=yahoo_finance_futures&limit=500').then(r => r.json()).then(c => {
      document.getElementById('comparison').innerHTML = `<div class="row"><span class="source">${esc(c.instrument_id)} · ${esc(c.primary_source)} vs ${esc(c.sources[1])}</span><span class="pill">never blended</span></div><dl><dt>Overlapping candles</dt><dd>${esc(c.overlap_count)}</dd><dt>Max close deviation</dt><dd>${Number(c.max_close_deviation_pct || 0).toFixed(4)}%</dd><dt>Provider symbols</dt><dd>${esc(Object.values(c.provider_symbols).join(' / '))}</dd></dl>`;
    });
  }
}).catch(error => { document.getElementById('sources').innerHTML = `<div class="card error">Health request failed: ${esc(error)}</div>`; });
</script></body></html>"""
