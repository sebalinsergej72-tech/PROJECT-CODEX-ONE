const getBaseUrl = (): string => {
  const raw = localStorage.getItem("bot_api_url") || "";
  return raw.replace(/\/+$/, "");
};

const getAuthHeaders = (): Record<string, string> => {
  const token = localStorage.getItem("dashboard_write_token") || "";
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["X-Dashboard-Token"] = token;
  return headers;
};

export interface DiscoveryStats {
  total_candidates: number;
  passed_trades: number;
  passed_win_rate: number;
  passed_profit_factor: number;
  passed_avg_size: number;
  passed_recency: number;
  passed_consecutive_losses: number;
  passed_wallet_age: number;
  passed_all_filters: number;
  enabled_wallets: number;
}

export interface BotStatus {
  dry_run: boolean;
  trading_enabled: boolean;
  scheduler_running: boolean;
  risk_mode: string;
  tracked_wallets: number;
  open_positions: number;
  total_equity_usd: number;
  exposure_usd: number;
  price_filter_enabled: boolean;
  high_conviction_boost_enabled: boolean;
  discovery_autoadd: boolean;
  daily_pnl_usd: number;
  cumulative_pnl_usd: number;
  last_trade_scan_at: string | null;
  live_started_at: string | null;
  discovery_scanned_candidates: number;
  discovery_passed_filters: number;
  discovery_filter_stats: Record<string, number>;
  last_discovery_stats?: DiscoveryStats;
  account_balances?: {
    source: string;
    free_balance_usd: number | null;
    positions_value_usd: number | null;
    total_balance_usd: number | null;
    positions_count: number;
    updated_at: string | null;
  };
}

export interface Trade {
  copied_at: string;
  status: string;
  market_id: string;
  side: string;
  price_cents: number;
  size_usd: number;
  wallet_address: string;
}

export interface Position {
  market_id: string;
  outcome: string;
  side: string;
  quantity: number;
  avg_price_cents: number;
  invested_usd: number;
  current_price_cents: number;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  updated_at: string;
}

export async function fetchStatus(): Promise<BotStatus> {
  const resp = await fetch(`${getBaseUrl()}/status`);
  if (!resp.ok) throw new Error(`Status: HTTP ${resp.status}`);
  return resp.json() as Promise<BotStatus>;
}

export async function fetchTrades(limit = 12): Promise<Trade[]> {
  const resp = await fetch(`${getBaseUrl()}/trades?limit=${limit}`);
  if (!resp.ok) throw new Error(`Trades: HTTP ${resp.status}`);
  const data = (await resp.json()) as { trades?: Trade[] };
  return data.trades || [];
}

export async function fetchPositions(limit = 12): Promise<Position[]> {
  const resp = await fetch(`${getBaseUrl()}/positions?open_only=true&limit=${limit}`);
  if (!resp.ok) throw new Error(`Positions: HTTP ${resp.status}`);
  const data = (await resp.json()) as { positions?: Position[] };
  return data.positions || [];
}

export async function postControl(path: string, payload: Record<string, unknown>) {
  const resp = await fetch(`${getBaseUrl()}${path}`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = (await resp.json().catch(() => ({ detail: "unknown_error" }))) as {
      detail?: string;
      error?: string;
      message?: string;
    };
    throw new Error(err.detail || err.error || err.message || `HTTP ${resp.status}`);
  }
  return resp.json() as Promise<Record<string, unknown>>;
}
