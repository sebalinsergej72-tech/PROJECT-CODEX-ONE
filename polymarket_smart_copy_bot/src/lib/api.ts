export const isTelegramWebApp = (): boolean => {
  return Boolean(window.Telegram?.WebApp);
};

const getBaseUrl = (): string => {
  if (isTelegramWebApp()) {
    return "";
  }
  const raw = localStorage.getItem("bot_api_url") || "";
  return raw.replace(/\/+$/, "");
};

const getStoredDashboardToken = (): string => {
  return (
    localStorage.getItem("dashboard_session_token") ||
    localStorage.getItem("dashboard_write_token") ||
    ""
  );
};

const getAuthHeaders = (): Record<string, string> => {
  const token = getStoredDashboardToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["X-Dashboard-Token"] = token;
  return headers;
};

export async function authenticateTelegramWebApp(initData: string) {
  const resp = await fetch(`${getBaseUrl()}/control/telegram-webapp/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: initData }),
  });
  if (!resp.ok) {
    const err = (await resp.json().catch(() => ({ detail: "telegram_auth_failed" }))) as {
      detail?: string;
    };
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json() as Promise<{ dashboard_token: string; expires_at: string }>;
}

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
  available_cash_usd?: number;
  open_order_reserve_usd?: number;
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
  seed_wallets?: {
    total: number;
    enabled: number;
    disabled: number;
    avg_score: number;
    avg_win_rate: number;
    avg_profit_factor: number;
    disable_reasons: Record<string, number>;
  };
  account_balances?: {
    source: string;
    free_balance_usd: number | null;
    net_free_balance_usd: number | null;
    open_orders_reserved_usd: number | null;
    positions_value_usd: number | null;
    total_balance_usd: number | null;
    tradable_collateral_usd?: number | null;
    funding_blocker?: string | null;
    onchain_funding?: {
      funder_pusd_balance_usd?: number | null;
      funder_usdce_balance_usd?: number | null;
      funder_usdc_balance_usd?: number | null;
      funder_pusd_allowance_exchange_usd?: number | null;
      funder_pusd_allowance_neg_risk_exchange_usd?: number | null;
      funder_pusd_allowance_neg_risk_adapter_usd?: number | null;
    } | null;
    balance_is_authoritative?: boolean;
    positions_count: number;
    open_orders_count: number;
    updated_at: string | null;
  };
}

export interface Trade {
  id?: number;
  copied_at?: string;
  status?: string;
  reason?: string | null;
  tx_hash?: string;
  source_timestamp?: string;
  market_id: string;
  market_title?: string;
  market_category?: string;
  token_id?: string;
  outcome?: string;
  side: string;
  price_cents: number;
  size_usd: number;
  wallet_address: string;
  order_id?: string | null;
  submitted_at?: string | null;
  filled_at?: string | null;
  canceled_at?: string | null;
  filled_quantity?: number;
  filled_size_usd?: number;
  filled_price_cents?: number;
}

export interface Position {
  id: number;
  market_id: string;
  market_title?: string;
  market_category?: string;
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

export interface OpenOrder {
  order_id: string;
  market_id: string | null;
  token_id?: string | null;
  side: string;
  price_cents: number;
  size_shares: number;
  notional_usd_estimate: number;
  created_at: string | null;
  trade_status?: string | null;
  wallet_address?: string | null;
  outcome?: string | null;
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

export async function fetchOpenOrders(limit = 12): Promise<OpenOrder[]> {
  const resp = await fetch(`${getBaseUrl()}/orders/open?limit=${limit}`);
  if (!resp.ok) throw new Error(`Open orders: HTTP ${resp.status}`);
  const data = (await resp.json()) as { orders?: OpenOrder[] };
  return data.orders || [];
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

export interface PortfolioSnapshot {
  taken_at: string;
  total_equity_usd: number;
  available_cash_usd: number;
  exposure_usd: number;
  cumulative_pnl_usd: number;
}

export async function fetchPortfolioHistory(hours = 168): Promise<PortfolioSnapshot[]> {
  const resp = await fetch(`${getBaseUrl()}/portfolio_history?hours=${hours}`);
  if (!resp.ok) throw new Error(`Portfolio History: HTTP ${resp.status}`);
  const data = (await resp.json()) as PortfolioSnapshot[];
  return data;
}

export interface WalletScore {
  wallet_address: string;
  label?: string;
  score: number;
  win_rate: number;
  roi_30d: number;
  total_volume_30d: number;
  trade_count_30d: number;
  trade_count_90d: number;
  total_volume_90d: number;
  profit_factor: number;
  avg_position_size: number;
}

export async function fetchLeaderboard(limit = 50): Promise<WalletScore[]> {
  const resp = await fetch(`${getBaseUrl()}/leaderboard?limit=${limit}`);
  if (!resp.ok) throw new Error(`Leaderboard: HTTP ${resp.status}`);
  const data = (await resp.json()) as WalletScore[];
  return data;
}

export async function closePosition(positionId: number): Promise<Record<string, unknown>> {
  const resp = await fetch(`${getBaseUrl()}/control/positions/${positionId}/close`, {
    method: "POST",
    headers: getAuthHeaders(),
  });
  if (!resp.ok) {
    let errDetail = `HTTP ${resp.status}`;
    try {
      const err = await resp.json();
      errDetail = err.detail || err.error || err.message || errDetail;
    } catch { }
    throw new Error(errDetail);
  }
  return resp.json();
}
