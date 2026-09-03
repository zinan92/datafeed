"""Chinese, read-only browser surface for the MVP health matrix."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/health-ui", response_class=HTMLResponse, include_in_schema=False)
async def health_ui() -> str:
    """Serve the dashboard shell; all facts are fetched from the matrix API."""

    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>市场数据健康监控</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#08111b; --panel:#101d2a; --panel-2:#0d1824; --line:#24364a;
      --text:#e8f1f8; --muted:#8da3b8; --accent:#67b7ff; --accent-2:#9a8cff;
      --ok:#42d392; --partial:#ffc857; --stale:#f4a261; --bad:#ff6b78;
    }
    * { box-sizing:border-box }
    body { margin:0; background:radial-gradient(circle at 10% 0%,#13263a 0,#08111b 40%);
      color:var(--text); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
    main { max-width:1500px; margin:0 auto; padding:28px 24px 54px }
    header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:18px }
    .eyebrow { color:var(--accent); font-size:12px; letter-spacing:.08em; text-transform:uppercase; margin-bottom:6px }
    h1 { margin:0 0 6px; font-size:30px; letter-spacing:-.02em }
    h2 { margin:0 0 12px; font-size:17px }
    p { margin:0; color:var(--muted) }
    .header-meta { min-width:230px; text-align:right; color:var(--muted); font-size:12px }
    .header-meta strong { display:block; color:var(--text); font-size:14px; margin-bottom:4px }
    .view-switch { display:flex; gap:8px; margin-top:12px }
    .view-switch a { border:1px solid var(--line); border-radius:7px; padding:6px 9px; color:var(--muted); text-decoration:none; font-size:12px }
    .view-switch a:hover { border-color:var(--accent); color:var(--text) }
    .banner { position:sticky; top:10px; z-index:5; display:none; margin:0 0 18px;
      padding:12px 14px; border:1px solid #71404a; border-radius:10px; background:#321b25; color:#ffdce1; }
    .banner.warn { border-color:#80652d; background:#332c18; color:#ffe5a5; }
    .banner.show { display:flex; align-items:center; gap:10px }
    .banner .dot { width:9px; height:9px; flex:none; border-radius:50%; background:var(--bad); box-shadow:0 0 14px var(--bad) }
    .banner.warn .dot { background:var(--partial); box-shadow:0 0 14px var(--partial) }
    .toolbar { display:flex; flex-wrap:wrap; justify-content:space-between; gap:12px; align-items:center;
      margin-bottom:18px; padding:11px 14px; background:rgba(16,29,42,.8); border:1px solid var(--line); border-radius:10px; }
    .toolbar span { color:var(--muted); font-size:12px }
    .toolbar label { display:flex; align-items:center; gap:6px }
    .toolbar input, .toolbar select { border:1px solid var(--line); border-radius:7px; padding:7px 10px; background:var(--panel-2); color:var(--text) }
    .toolbar input { width:150px }
    .coverage-grid { display:grid; grid-template-columns:repeat(5,minmax(155px,1fr)); gap:10px; margin-bottom:26px }
    .card { background:linear-gradient(145deg,rgba(16,29,42,.98),rgba(12,23,34,.98)); border:1px solid var(--line); border-radius:12px; padding:14px }
    .coverage-card { min-height:116px }
    .coverage-card .label { color:var(--muted); font-size:12px }
    .coverage-card .ratio { display:flex; align-items:baseline; gap:7px; margin:8px 0 4px }
    .coverage-card .ratio strong { font-size:26px; letter-spacing:-.03em }
    .coverage-card .ratio span { color:var(--muted); font-size:12px }
    .coverage-card .counts { color:var(--muted); font-size:12px }
    .progress { height:4px; border-radius:99px; background:#1b2b3b; overflow:hidden; margin-top:11px }
    .progress i { display:block; height:100%; border-radius:inherit; background:linear-gradient(90deg,var(--accent),var(--accent-2)); }
    .section { margin-top:24px }
    .section-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:12px }
    .section-head small { color:var(--muted); font-size:12px }
    .matrix-wrap { overflow:auto; border:1px solid var(--line); border-radius:12px; background:var(--panel-2) }
    table { width:100%; min-width:940px; border-collapse:collapse }
    th,td { padding:10px 11px; border-bottom:1px solid rgba(36,54,74,.7); text-align:left; vertical-align:middle }
    th { position:sticky; top:0; z-index:2; background:#152639; color:var(--muted); font-size:12px; font-weight:600; white-space:nowrap }
    tr:last-child td { border-bottom:0 }
    .asset { min-width:185px }
    .asset strong { display:block; font-size:13px }
    .asset small { display:block; color:var(--muted); margin-top:2px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    .group-row td { padding:8px 11px; background:#111f2e; color:var(--accent); font-weight:700 }
    .group-toggle { border:0; padding:0; background:transparent; color:inherit; font:inherit; cursor:pointer }
    .cell { width:100%; min-width:118px; border:1px solid transparent; border-radius:8px; padding:7px 8px; text-align:left;
      background:#152335; color:var(--text); cursor:pointer; transition:border-color .15s,transform .15s; }
    .cell:hover { border-color:var(--accent); transform:translateY(-1px) }
    .cell .cell-top { display:flex; justify-content:space-between; gap:6px; align-items:center }
    .cell .state { font-weight:700; font-size:12px }
    .cell .age { color:var(--muted); font-size:11px; margin-top:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    .cell.ready { background:#103126; color:#bff4d9 }
    .cell.partial { background:#332c18; color:#ffe5a5 }
    .cell.stale { background:#332718; color:#ffd0a0 }
    .cell.failed, .cell.blocked, .cell.unavailable { background:#351c25; color:#ffd0d6 }
    .cell.not_applicable { background:#17202a; color:#8ea1b1; cursor:default }
    .empty { color:var(--muted); padding:22px; text-align:center }
    .lower-grid { display:grid; grid-template-columns:minmax(0,1.45fr) minmax(300px,.85fr); gap:14px; }
    .run-table { min-width:700px }
    .run-table td, .run-table th { white-space:nowrap }
    .run-status { font-weight:700 }
    .infra-grid { display:grid; gap:10px }
    .infra-item { display:flex; justify-content:space-between; gap:12px; padding-bottom:10px; border-bottom:1px solid rgba(36,54,74,.7) }
    .infra-item:last-child { border-bottom:0; padding-bottom:0 }
    .infra-item span { color:var(--muted) }
    .infra-item strong { text-align:right }
    .drawer-backdrop { position:fixed; inset:0; display:none; z-index:10; background:rgba(1,6,12,.68) }
    .drawer-backdrop.open { display:block }
    .drawer { position:absolute; top:0; right:0; height:100%; width:min(480px,94vw); overflow:auto; padding:22px;
      background:#0e1a27; border-left:1px solid var(--line); box-shadow:-18px 0 50px rgba(0,0,0,.35) }
    .drawer-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; margin-bottom:18px }
    .drawer-head h2 { margin:0 }
    .close { border:1px solid var(--line); border-radius:7px; background:transparent; color:var(--muted); padding:5px 9px; cursor:pointer }
    .detail-list { display:grid; grid-template-columns:140px minmax(0,1fr); gap:9px 12px; margin:0 }
    .detail-list dt { color:var(--muted) }
    .detail-list dd { margin:0; overflow-wrap:anywhere; color:var(--text) }
    .detail-list code { color:#c8d9e8; white-space:pre-wrap; font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace }
    @media (max-width:900px) { header { flex-direction:column } .header-meta { min-width:0; text-align:left }
      .coverage-grid { grid-template-columns:repeat(2,minmax(145px,1fr)) } .lower-grid { grid-template-columns:1fr } }
    @media (max-width:520px) { main { padding:22px 14px 42px } h1 { font-size:25px } .coverage-grid { grid-template-columns:1fr 1fr } }
  </style>
</head>
<body>
<main>
  <header>
    <div><div class="eyebrow">市场数据管线 / 运行监控</div><h1>资产 × 时间级别健康矩阵</h1>
      <nav class="view-switch" aria-label="数据视图"><a href="/health-ui">Screening 观察</a><a href="/health-ui?view=combined">Watchlist + Screening</a></nav>
      <p>只读查看最近一次持久化运行、数据质量和覆盖情况。页面每 30 秒自动读取。</p></div>
    <div class="header-meta"><strong id="overall">等待首次读取</strong><span id="last-success">尚未取得成功快照</span></div>
  </header>
  <div id="banner" class="banner" role="status"><i class="dot"></i><span id="banner-text"></span></div>
  <div class="toolbar"><span id="snapshot-meta">正在连接健康矩阵…</span>
    <label><span>搜索：</span><input id="search" aria-label="搜索" type="search" placeholder="代码或名称" autocomplete="off"></label>
    <label id="dataset-filter-wrap"><span>数据集：</span><select id="dataset-filter" aria-label="数据集"><option value="all">全部数据</option><option value="screening">Screening</option><option value="watchlist">Watchlist</option></select></label>
    <label><span>市场：</span><select id="market-filter" aria-label="市场"><option value="all">全部市场</option><option value="a_share">A 股</option><option value="us_stock">美股</option><option value="cross_market">跨市场</option></select></label>
    <label><span>时间级别：</span><select id="timeframe-filter" aria-label="时间级别"><option value="all">全部级别</option><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option><option value="1d">日线</option><option value="1w">周线</option></select></label>
    <label><span>状态：</span><select id="filter" aria-label="状态"><option value="all">全部状态</option><option value="ready">正常</option><option value="partial">部分</option><option value="stale">过期</option><option value="failed">失败</option><option value="blocked">阻塞</option><option value="unavailable">不可用</option><option value="not_applicable">不适用</option></select></label></div>

  <section><div class="section-head"><h2>覆盖概览</h2><small id="coverage-meta">—</small></div><div id="coverage" class="coverage-grid"></div></section>

  <section class="section"><div class="section-head"><h2>资产 × 时间级别明细</h2><small>点击单元格查看来源、质量和水位证据</small></div>
    <div class="matrix-wrap"><table id="matrix"><thead><tr><th class="asset">资产</th><th>15 分钟</th><th>1 小时</th><th>4 小时</th><th>日线</th><th>周线</th></tr></thead><tbody id="matrix-body"><tr><td colspan="6" class="empty">正在读取数据…</td></tr></tbody></table></div></section>

  <section class="section lower-grid"><div><div class="section-head"><h2>最近一次运行</h2><small id="next-run-meta">按时间倒序展示</small></div>
      <div class="matrix-wrap"><table class="run-table"><thead><tr><th>运行编号</th><th>状态</th><th>开始</th><th>结束</th><th>覆盖/尝试</th><th>水位推进</th><th>失败原因</th></tr></thead><tbody id="runs-body"><tr><td colspan="7" class="empty">暂无运行记录</td></tr></tbody></table></div></div>
    <div><div class="section-head"><h2>基础设施</h2><small>不包含密钥和原始路径</small></div><div id="infrastructure" class="card infra-grid"><div class="empty">暂无基础设施事实</div></div></div></section>
</main>

<div id="drawer-backdrop" class="drawer-backdrop" aria-hidden="true"><aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
  <div class="drawer-head"><h2 id="drawer-title">单元格详情</h2><button class="close" id="close-drawer" type="button">关闭</button></div><dl id="detail-list" class="detail-list"></dl>
</aside></div>

<script>
(() => {
  const VIEW = new URLSearchParams(window.location.search).get('view') || 'screening';
  const API = VIEW === 'combined' ? '/api/health/combined-matrix' : '/api/mvp/health/matrix';
  const POLL_MS = 30000;
  const TIMEOUT_MS = 10000;
  const MAX_SNAPSHOT_MS = 900000;
  const timeframes = ['15m','1h','4h','1d','1w'];
  const timeframeLabels = {'15m':'15 分钟','1h':'1 小时','4h':'4 小时','1d':'日线','1w':'周线'};
  const statusLabels = {ready:'正常',partial:'部分',stale:'过期',failed:'失败',blocked:'阻塞',unavailable:'不可用',not_applicable:'不适用'};
  const reasonLabels = {entitlement_blocked:'授权未核实',entitlement_unverified:'授权未核实',entitlement_expired:'授权已过期',persistence_not_allowed:'不允许持久化',derived_not_allowed:'不允许派生',timeframe_not_permitted:'级别未授权',timeframe_permission_unverified:'级别授权未核实'};
  const universeLabels = {a_share:'A 股',us_stock:'美股',cross_market:'跨市场'};
  const queryParams = new URLSearchParams(window.location.search);
  const initialDataset = queryParams.get('dataset') || 'all';
  let latestSnapshot = null;
  let latestReceivedAt = 0;
  let loading = false;
  const collapsedGroups = new Set();

  const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const fmtTime = value => { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString('zh-CN',{hour12:false}); };
  const fmtCount = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
  const statusText = status => statusLabels[status] || status || '未知';
  const validSnapshot = data => {
    if (!data || !Array.isArray(data.cells) || !data.coverage || typeof data.coverage !== 'object' || !data.refresh || !data.worker || typeof data.worker !== 'object' || !Array.isArray(data.runs) || !data.infrastructure || typeof data.infrastructure !== 'object' || !Object.prototype.hasOwnProperty.call(statusLabels, data.status)) return false;
    if (!timeframes.every(tf => data.coverage[tf] && Number.isFinite(Number(data.coverage[tf].applicable)) && Number.isFinite(Number(data.coverage[tf].not_applicable)))) return false;
    return data.cells.every(cell => timeframes.includes(cell.timeframe) && ['applicable','not_applicable'].includes(cell.applicability) && Object.prototype.hasOwnProperty.call(statusLabels, cell.status));
  };
  const showBanner = (text, severity = 'error') => {
    const banner = document.getElementById('banner');
    document.getElementById('banner-text').textContent = text;
    banner.classList.toggle('warn', severity === 'warn');
    banner.classList.add('show');
  };
  const hideBanner = () => document.getElementById('banner').classList.remove('show');

  function renderCoverage(snapshot) {
    const host = document.getElementById('coverage');
    host.innerHTML = timeframes.map(tf => {
      const item = snapshot.coverage[tf] || {};
      const applicable = Number(item.applicable || 0);
      const ready = Number(item.ready || 0);
      const dataReady = Number(item.technical_ready ?? ready);
      const ratio = applicable ? Math.round(dataReady / applicable * 100) : 0;
      const blocked = Number(item.blocked || 0);
      const failed = Number(item.failed || 0);
      const problem = ['stale','partial','unavailable'].reduce((sum, key) => sum + Number(item[key] || 0), 0);
      const details = [blocked ? `阻塞 ${blocked}` : '', failed ? `失败 ${failed}` : '', problem ? `其他需关注 ${problem}` : ''].filter(Boolean).join(' · ') || '没有异常单元格';
      return `<article class="card coverage-card"><div class="label">${timeframeLabels[tf]}</div><div class="ratio"><strong>${ratio}%</strong><span>${dataReady}/${applicable} 有数据</span></div><div class="counts">${details} · 正常 ${ready} · 不适用 ${Number(item.not_applicable || 0)} 个</div><div class="progress"><i style="width:${ratio}%"></i></div></article>`;
    }).join('');
    const manifestMeta = snapshot.manifest_versions
      ? `Screening ${esc(snapshot.manifest_versions.screening)} + Watchlist ${esc(snapshot.manifest_versions.watchlist)}`
      : `清单 ${esc(snapshot.manifest_version)}`;
    document.getElementById('coverage-meta').textContent = `共 ${fmtCount(snapshot.cells.length)} 个单元格 · ${manifestMeta}`;
  }

  const universeFor = cell => {
    if (cell.universe === 'watchlist') {
      if (String(cell.instrument_id || '').startsWith('WATCH.CROSS.')) return 'cross_market';
      if (String(cell.instrument_id || '').startsWith('WATCH.CN.')) return 'a_share';
      if (String(cell.instrument_id || '').startsWith('WATCH.US.') || String(cell.instrument_id || '').startsWith('WATCH.KR.')) return 'us_stock';
    }
    return cell.asset_class === 'a_share' ? 'a_share' : cell.asset_class === 'us_stock' ? 'us_stock' : 'cross_market';
  };

  function renderMatrix(snapshot) {
    const query = document.getElementById('search').value.trim().toLowerCase();
    const market = document.getElementById('market-filter').value;
    const dataset = document.getElementById('dataset-filter').value;
    const timeframeFilter = document.getElementById('timeframe-filter').value;
    const statusFilter = document.getElementById('filter').value;
    const grouped = new Map();
    snapshot.cells.forEach(cell => {
      const universe = universeFor(cell);
      const matchesText = !query || `${cell.display_symbol || ''} ${cell.display_name || ''}`.toLowerCase().includes(query);
      if (!matchesText || (dataset !== 'all' && cell.dataset !== dataset) || (market !== 'all' && universe !== market) || (statusFilter !== 'all' && cell.status !== statusFilter)) return;
      const key = cell.instrument_id || cell.display_symbol;
      if (!grouped.has(universe)) grouped.set(universe, new Map());
      if (!grouped.get(universe).has(key)) grouped.get(universe).set(key, {symbol:cell.display_symbol, name:cell.display_name, cells:{}});
      grouped.get(universe).get(key).cells[cell.timeframe] = cell;
    });
    const sections = Array.from(grouped.entries()).filter(([, rows]) => Array.from(rows.values()).some(row => timeframeFilter === 'all' || row.cells[timeframeFilter]));
    const html = sections.map(([universe, rows]) => {
      const collapsed = collapsedGroups.has(universe);
      const rowHtml = collapsed ? '' : Array.from(rows.values()).filter(row => timeframeFilter === 'all' || row.cells[timeframeFilter]).map(row => `<tr><td class="asset"><strong>${esc(row.symbol)}</strong><small>${esc(row.name)}</small></td>${timeframes.map(tf => {
        const cell = row.cells[tf];
        if (!cell) return '<td><div class="empty">—</div></td>';
        const latest = reasonLabels[cell.status_reason] || (cell.latest_closed_timestamp ? fmtTime(cell.latest_closed_timestamp) : '暂无收盘数据');
        return `<td><button class="cell ${esc(cell.status)}" type="button" data-cell="${esc(JSON.stringify(cell))}"><span class="cell-top"><span class="state">${esc(statusText(cell.status))}</span><span>${esc(timeframeLabels[tf])}</span></span><span class="age">${esc(latest)}</span></button></td>`;
      }).join('')}</tr>`).join('');
      return `<tr class="group-row"><td colspan="6"><button class="group-toggle" type="button" data-group-toggle="${esc(universe)}">${collapsed ? '展开' : '收起'} · ${esc(universeLabels[universe])}（${rows.size} 个资产）</button></td></tr>${rowHtml}`;
    }).join('');
    document.getElementById('matrix-body').innerHTML = html || '<tr><td colspan="6" class="empty">当前筛选没有需要展示的资产</td></tr>';
    document.querySelectorAll('[data-cell]').forEach(button => button.addEventListener('click', () => openDetail(JSON.parse(button.dataset.cell))));
    document.querySelectorAll('[data-group-toggle]').forEach(button => button.addEventListener('click', () => {
      const key = button.dataset.groupToggle;
      if (collapsedGroups.has(key)) collapsedGroups.delete(key); else collapsedGroups.add(key);
      renderMatrix(snapshot);
    }));
  }

  function renderRuns(snapshot) {
    const rows = snapshot.runs || [];
    const nextDue = (snapshot.worker || {}).next_due_at;
    document.getElementById('next-run-meta').textContent = nextDue ? `下一次运行 ${fmtTime(nextDue)}` : '最近 24 小时 · 按时间倒序展示';
    document.getElementById('runs-body').innerHTML = rows.length ? rows.map(run => {
      const failure = run.error && typeof run.error === 'object' ? run.error.message : run.error;
      return `<tr><td title="${esc(run.run_id)}">${esc(String(run.run_id || '—').slice(0,18))}</td><td class="run-status">${esc(statusText(run.status === 'success' ? 'ready' : run.status))}</td><td>${esc(fmtTime(run.started_at))}</td><td>${esc(fmtTime(run.completed_at))}</td><td>${esc(fmtCount(run.observation_count))}</td><td>${esc(fmtCount(run.watermark_count))}</td><td title="${esc(failure)}">${esc(failure || '—')}</td></tr>`;
    }).join('') : `<tr><td colspan="7" class="empty">暂无最近 24 小时运行记录${nextDue ? ` · 下一次 ${esc(fmtTime(nextDue))}` : ''}</td></tr>`;
  }

  function renderInfrastructure(snapshot) {
    const infra = snapshot.infrastructure || {};
    const worker = infra.worker || snapshot.worker || {};
    const database = infra.database || {};
    const databases = infra.databases || {};
    const backup = infra.nas_backup || {};
    const label = value => value === 'ready' || value === 'ok' || value === 'last_run' ? '正常' : statusText(value || 'unavailable');
    const databaseItems = Object.keys(databases).length
      ? Object.entries(databases).map(([name, item]) => [name === 'market_data' ? 'Market Data 数据库' : 'Screening 数据库', label(item.status), '只读 SQLite（路径已隐藏）'])
      : [['本地数据库', label(database.status), database.filesystem === 'local' ? '本地 SQLite（路径已隐藏）' : '路径已隐藏']];
    document.getElementById('infrastructure').innerHTML = [
      ['采集任务', label(worker.status), worker.last_success_at ? `上次成功 ${fmtTime(worker.last_success_at)}` : '尚无成功运行'],
      ...databaseItems,
      ['SSD 挂载保护', label((infra.ssd_mount_guard || {}).status), '仅展示保护状态'],
      ['NAS 备份', label(backup.status), backup.last_backup_at ? `上次备份 ${fmtTime(backup.last_backup_at)}` : '尚无备份证据']
    ].map(item => `<div class="infra-item"><span>${esc(item[0])}<br><small>${esc(item[2])}</small></span><strong>${esc(item[1])}</strong></div>`).join('');
  }

  function render(snapshot) {
    latestSnapshot = snapshot;
    renderCoverage(snapshot); renderMatrix(snapshot); renderRuns(snapshot); renderInfrastructure(snapshot);
    const applicable = snapshot.cells.filter(cell => cell.applicability === 'applicable');
    const blocked = applicable.filter(cell => cell.status === 'blocked').length;
    const failed = applicable.filter(cell => cell.status === 'failed').length;
    const unavailable = applicable.filter(cell => cell.status === 'unavailable').length;
    const technicalReady = applicable.filter(cell => cell.technical_status === 'ready' || cell.status === 'ready').length;
    const mixedTechnicalState = technicalReady > 0 && (blocked || failed || unavailable);
    const overallText = mixedTechnicalState && snapshot.status === 'failed' && blocked && !failed
      ? '总体状态：部分可用（含授权阻塞）'
      : snapshot.status === 'failed' && blocked && !failed
        ? '总体状态：授权阻塞'
        : `总体状态：${statusText(snapshot.status)}`;
    document.getElementById('overall').textContent = overallText;
    document.getElementById('snapshot-meta').textContent = `数据时间 ${fmtTime(snapshot.as_of)} · 自动读取间隔 30 秒 · 请求上限 10 秒`;
    if (snapshot.status === 'failed') {
      if (blocked && !failed && technicalReady) {
        const details = [`已有 ${technicalReady} 个单元格有技术数据`, `${blocked} 个单元格因授权未核实`];
        if (unavailable) details.push(`${unavailable} 个单元格暂无数据`);
        showBanner(`${details.join('；')}。不是采集程序崩溃，请按资产和时间级别查看`, 'warn');
      } else if (blocked && !failed) {
        showBanner(`当前 ${blocked} 个单元格因授权阻塞，尚无可展示数据；不是采集程序崩溃`, 'error');
      }
      else if (blocked) showBanner(`当前有 ${failed} 个采集失败单元格和 ${blocked} 个授权阻塞单元格，请分别查看详情`, 'error');
      else showBanner(`当前有 ${failed} 个采集失败单元格，请查看矩阵详情`, 'error');
    } else if (snapshot.status === 'partial') {
      showBanner('数据源存在过期、部分或不可用单元格，请查看覆盖概览', 'warn');
    } else {
      hideBanner();
    }
  }

  function openDetail(cell) {
    const rows = [
      ['状态', statusText(cell.status)], ['状态原因', cell.status_reason], ['资产', `${cell.display_symbol || '—'} · ${cell.display_name || '—'}`],
      ['时间级别', timeframeLabels[cell.timeframe] || cell.timeframe], ['适用性', cell.applicability === 'not_applicable' ? '不适用' : '适用'],
      ['来源标识', cell.source_id], ['供应商代码', cell.provider_symbol], ['来源模式', cell.source_mode],
      ['最近收盘', cell.latest_closed_timestamp ? fmtTime(cell.latest_closed_timestamp) : null], ['存储行数', fmtCount(cell.row_count)],
      ['是否聚合', cell.is_derived == null ? null : (cell.is_derived ? '是' : '否')], ['最近尝试', fmtTime(cell.last_attempt_at)],
      ['最近成功', fmtTime(cell.last_success_at)], ['资产元数据', cell.metadata], ['质量', cell.quality], ['水位', cell.watermark], ['转换凭证', cell.transform], ['策略信息', cell.policy], ['错误', cell.error]
    ];
    document.getElementById('drawer-title').textContent = `${cell.display_symbol || '资产'} · ${timeframeLabels[cell.timeframe] || cell.timeframe}`;
    document.getElementById('detail-list').innerHTML = rows.map(([key,value]) => `<dt>${esc(key)}</dt><dd>${value && typeof value === 'object' ? `<code>${esc(JSON.stringify(value,null,2))}</code>` : esc(value)}</dd>`).join('');
    const drawer = document.getElementById('drawer-backdrop'); drawer.classList.add('open'); drawer.setAttribute('aria-hidden','false');
  }
  const closeDetail = () => { const drawer = document.getElementById('drawer-backdrop'); drawer.classList.remove('open'); drawer.setAttribute('aria-hidden','true'); };

  async function fetchHealth() {
    if (loading) return; loading = true;
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await fetch(`${API}?_=${Date.now()}`, {cache:'no-store', signal:controller.signal, headers:{'Accept':'application/json'}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json(); if (!validSnapshot(data)) throw new Error('响应结构不完整');
      latestReceivedAt = Date.now(); render(data);
      document.getElementById('last-success').textContent = `最近成功读取 ${new Date(latestReceivedAt).toLocaleString('zh-CN',{hour12:false})}`;
    } catch (error) {
      const message = error.name === 'AbortError' ? '健康矩阵读取超过 10 秒' : `健康矩阵读取失败：${error.message || error}`;
      if (!latestSnapshot) {
        document.getElementById('overall').textContent = '监控服务不可用';
        document.getElementById('snapshot-meta').textContent = '尚未取得可展示的健康快照';
        document.getElementById('coverage').innerHTML = '<div class="card empty">暂无可验证的矩阵数据</div>';
        document.getElementById('matrix-body').innerHTML = '<tr><td colspan="6" class="empty">监控服务不可用，暂不展示推测数据</td></tr>';
      } else {
        const age = Date.now() - latestReceivedAt;
        if (age > MAX_SNAPSHOT_MS) {
          latestSnapshot = null;
          document.getElementById('overall').textContent = '快照已过期';
          document.getElementById('snapshot-meta').textContent = '最近成功快照已超过 15 分钟，暂不继续展示';
          document.getElementById('coverage').innerHTML = '<div class="card empty">快照已过期，等待新的可验证数据</div>';
          document.getElementById('matrix-body').innerHTML = '<tr><td colspan="6" class="empty">快照已过期</td></tr>';
        } else {
          document.getElementById('snapshot-meta').textContent = `读取失败，保留最近快照 · 已过 ${Math.round(age / 1000)} 秒`;
        }
      }
      showBanner(message);
    } finally { clearTimeout(timeout); loading = false; }
  }

  const datasetFilter = document.getElementById('dataset-filter');
  if (VIEW === 'combined' && ['all','screening','watchlist'].includes(initialDataset)) {
    datasetFilter.value = initialDataset;
  } else if (VIEW !== 'combined') {
    datasetFilter.value = 'all';
    document.getElementById('dataset-filter-wrap').hidden = true;
  }
  ['search','dataset-filter','market-filter','timeframe-filter','filter'].forEach(id => {
    document.getElementById(id).addEventListener(id === 'search' ? 'input' : 'change', () => { if (latestSnapshot) renderMatrix(latestSnapshot); });
  });
  document.getElementById('close-drawer').addEventListener('click', closeDetail);
  document.getElementById('drawer-backdrop').addEventListener('click', event => { if (event.target.id === 'drawer-backdrop') closeDetail(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDetail(); });
  fetchHealth(); setInterval(fetchHealth, POLL_MS);
})();
</script>
</body>
</html>"""
