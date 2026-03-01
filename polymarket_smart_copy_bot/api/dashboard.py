from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from config.settings import settings

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    refresh_ms = max(3, settings.dashboard_refresh_seconds) * 1000
    token_required = "true" if bool(settings.dashboard_write_token) else "false"
    html = f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Polymarket Smart Copy Bot Dashboard</title>
  <style>
    :root {{
      --bg:#0b1020;
      --panel:#111931;
      --panel-2:#162345;
      --text:#eaf0ff;
      --muted:#9fb0d9;
      --good:#21c97a;
      --bad:#f16067;
      --warn:#f5b85a;
      --accent:#4f83ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace; background: radial-gradient(circle at 15% 20%, #1b2d61, #0b1020 55%); color: var(--text); }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    h1 {{ margin:0 0 14px; font-size: 24px; }}
    .row {{ display:grid; gap:12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-bottom: 12px; }}
    .card {{ background: linear-gradient(180deg, var(--panel), var(--panel-2)); border: 1px solid #2a3c71; border-radius: 14px; padding: 12px; }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .value {{ font-size: 20px; margin-top: 6px; }}
    .good {{ color: var(--good); }} .bad {{ color: var(--bad); }} .warn {{ color: var(--warn); }}
    .controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin: 10px 0 14px; }}
    button {{ border: 0; border-radius: 10px; padding: 10px 14px; font-weight: 700; cursor: pointer; }}
    .start {{ background: var(--good); color:#062612; }}
    .stop {{ background: var(--bad); color:#32080d; }}
    .refresh {{ background: var(--accent); color:#041535; }}
    input {{ border:1px solid #2a3c71; border-radius:8px; background:#0f1730; color:var(--text); padding:8px 10px; min-width:220px; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ border-bottom:1px solid #2a3c71; padding:8px 6px; text-align:left; font-size: 13px; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
    .split {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }}
    .muted {{ color: var(--muted); font-size:12px; }}
    @media (max-width: 960px) {{ .split {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>Polymarket Smart Copy Bot</h1>

    <div class=\"controls\">
      <button class=\"start\" onclick=\"setTrading(true)\">Start Trading</button>
      <button class=\"stop\" onclick=\"setTrading(false)\">Stop Trading</button>
      <button class=\"refresh\" onclick=\"loadAll()\">Refresh now</button>
      <input id=\"token\" type=\"password\" placeholder=\"Dashboard write token (optional)\" />
      <span id=\"msg\" class=\"muted\"></span>
    </div>

    <div class=\"row\">
      <div class=\"card\"><div class=\"label\">Engine</div><div class=\"value\" id=\"engine\">-</div></div>
      <div class=\"card\"><div class=\"label\">Trading</div><div class=\"value\" id=\"trading\">-</div></div>
      <div class=\"card\"><div class=\"label\">Risk Mode</div><div class=\"value\" id=\"mode\">-</div></div>
      <div class=\"card\"><div class=\"label\">Wallets Tracked</div><div class=\"value\" id=\"wallets\">-</div></div>
      <div class=\"card\"><div class=\"label\">Open Positions</div><div class=\"value\" id=\"openPos\">-</div></div>
      <div class=\"card\"><div class=\"label\">Exposure</div><div class=\"value\" id=\"exposure\">-</div></div>
      <div class=\"card\"><div class=\"label\">Daily PnL</div><div class=\"value\" id=\"dailyPnl\">-</div></div>
      <div class=\"card\"><div class=\"label\">Cumulative PnL</div><div class=\"value\" id=\"cumPnl\">-</div></div>
    </div>

    <div class=\"split\">
      <div class=\"card\">
        <div class=\"label\">Recent Trades</div>
        <table>
          <thead>
            <tr><th>Time</th><th>Status</th><th>Market</th><th>Side</th><th>Size</th><th>Wallet</th></tr>
          </thead>
          <tbody id=\"tradesBody\"></tbody>
        </table>
      </div>
      <div class=\"card\">
        <div class=\"label\">Open Positions</div>
        <table>
          <thead>
            <tr><th>Market</th><th>Outcome</th><th>Invested</th><th>U-PnL</th><th>Updated</th></tr>
          </thead>
          <tbody id=\"positionsBody\"></tbody>
        </table>
      </div>
    </div>
  </div>

<script>
const REFRESH_MS = {refresh_ms};
const TOKEN_REQUIRED = {token_required};

function shortAddr(v) {{
  if (!v) return "-";
  return v.length < 12 ? v : `${{v.slice(0, 6)}}...${{v.slice(-4)}}`;
}}
function money(v) {{
  const n = Number(v || 0);
  return `$${{n.toFixed(2)}}`;
}}
function toneForPnl(v) {{
  const n = Number(v || 0);
  if (n > 0) return "good";
  if (n < 0) return "bad";
  return "";
}}
function setMsg(text, isError=false) {{
  const el = document.getElementById("msg");
  el.textContent = text || "";
  el.className = isError ? "bad" : "muted";
}}
function authHeaders() {{
  const token = localStorage.getItem("dashboard_write_token") || "";
  const headers = {{"Content-Type": "application/json"}};
  if (token) headers["X-Dashboard-Token"] = token;
  return headers;
}}

async function loadAll() {{
  try {{
    const [statusResp, tradesResp, positionsResp] = await Promise.all([
      fetch('/status'),
      fetch('/trades?limit=12'),
      fetch('/positions?open_only=true&limit=12'),
    ]);

    const status = await statusResp.json();
    const trades = await tradesResp.json();
    const positions = await positionsResp.json();

    renderStatus(status);
    renderTrades(trades.trades || []);
    renderPositions(positions.positions || []);
    setMsg(`Updated: ${{new Date().toLocaleTimeString()}}`);
  }} catch (err) {{
    setMsg(`Load failed: ${{err}}`, true);
  }}
}}

function renderStatus(s) {{
  const dry = Boolean(s.dry_run);
  const trading = Boolean(s.trading_enabled);

  document.getElementById('engine').textContent = dry ? 'DRY RUN' : 'LIVE';
  const tr = document.getElementById('trading');
  tr.textContent = trading ? 'ENABLED' : 'PAUSED';
  tr.className = `value ${{trading ? 'good' : 'warn'}}`;

  document.getElementById('mode').textContent = String(s.risk_mode || '-').toUpperCase();
  document.getElementById('wallets').textContent = String(s.tracked_wallets ?? '-');
  document.getElementById('openPos').textContent = String(s.open_positions ?? '-');
  document.getElementById('exposure').textContent = money(s.exposure_usd || 0);

  const daily = document.getElementById('dailyPnl');
  daily.textContent = money(s.daily_pnl_usd || 0);
  daily.className = `value ${{toneForPnl(s.daily_pnl_usd)}}`;

  const cum = document.getElementById('cumPnl');
  cum.textContent = money(s.cumulative_pnl_usd || 0);
  cum.className = `value ${{toneForPnl(s.cumulative_pnl_usd)}}`;
}}

function renderTrades(rows) {{
  const body = document.getElementById('tradesBody');
  body.innerHTML = '';
  if (!rows.length) {{
    body.innerHTML = '<tr><td colspan="6" class="muted">No trades yet</td></tr>';
    return;
  }}
  for (const row of rows) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${{(row.copied_at || '-').replace('T', ' ').slice(0, 19)}}</td>
      <td>${{String(row.status || '-').toUpperCase()}}</td>
      <td>${{row.market_id || '-'}}</td>
      <td>${{String(row.side || '-').toUpperCase()}} @ ${{Number(row.price_cents || 0).toFixed(1)}}c</td>
      <td>${{money(row.size_usd)}}</td>
      <td>${{shortAddr(row.wallet_address || '')}}</td>
    `;
    body.appendChild(tr);
  }}
}}

function renderPositions(rows) {{
  const body = document.getElementById('positionsBody');
  body.innerHTML = '';
  if (!rows.length) {{
    body.innerHTML = '<tr><td colspan="5" class="muted">No open positions</td></tr>';
    return;
  }}
  for (const row of rows) {{
    const tr = document.createElement('tr');
    const uPnl = Number(row.unrealized_pnl_usd || 0);
    tr.innerHTML = `
      <td>${{row.market_id || '-'}}</td>
      <td>${{row.outcome || '-'}}</td>
      <td>${{money(row.invested_usd)}}</td>
      <td class="${{toneForPnl(uPnl)}}">${{money(uPnl)}}</td>
      <td>${{(row.updated_at || '-').replace('T', ' ').slice(0, 19)}}</td>
    `;
    body.appendChild(tr);
  }}
}}

async function setTrading(enabled) {{
  try {{
    const tokenInput = document.getElementById('token').value.trim();
    if (tokenInput) {{
      localStorage.setItem('dashboard_write_token', tokenInput);
    }}

    if (TOKEN_REQUIRED && !localStorage.getItem('dashboard_write_token')) {{
      setMsg('Write token is required for control actions.', true);
      return;
    }}

    const resp = await fetch('/control/trading', {{
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({{enabled, run_now: enabled}}),
    }});

    if (!resp.ok) {{
      const err = await resp.json().catch(() => ({{detail:'unknown_error'}}));
      throw new Error(err.detail || `HTTP ${{resp.status}}`);
    }}

    const data = await resp.json();
    setMsg(`Trading: ${{data.trading_enabled ? 'ENABLED' : 'PAUSED'}}`);
    await loadAll();
  }} catch (err) {{
    setMsg(`Action failed: ${{err.message || err}}`, true);
  }}
}}

(function init() {{
  const cached = localStorage.getItem('dashboard_write_token');
  if (cached) document.getElementById('token').value = cached;
  loadAll();
  setInterval(loadAll, REFRESH_MS);
}})();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)
