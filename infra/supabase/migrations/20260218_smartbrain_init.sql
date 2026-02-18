create extension if not exists "pgcrypto";

create table if not exists market_data (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  exchange text not null,
  timeframe text not null,
  open numeric,
  high numeric,
  low numeric,
  close numeric,
  volume numeric,
  orderbook_imbalance numeric,
  funding_rate numeric,
  open_interest numeric,
  liquidations numeric,
  created_at timestamptz not null default now()
);
create index if not exists idx_market_data_symbol_ts on market_data(symbol, ts desc);

create table if not exists sentiment_scores (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  source text not null,
  sentiment_score numeric not null,
  confidence numeric,
  summary text,
  created_at timestamptz not null default now()
);

create table if not exists macro_indicators (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  btc_dominance numeric,
  fear_greed numeric,
  total_market_cap numeric,
  alt_corr_index numeric,
  onchain_signal jsonb,
  created_at timestamptz not null default now()
);

create table if not exists feature_vectors (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  timeframe text not null,
  features jsonb not null,
  regime text,
  created_at timestamptz not null default now()
);
create index if not exists idx_feature_vectors_symbol_ts on feature_vectors(symbol, ts desc);

create table if not exists experience_replay (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  symbol text not null,
  mode text not null check (mode in ('paper', 'live')),
  state jsonb not null,
  action jsonb not null,
  outcome_1h jsonb,
  outcome_4h jsonb,
  outcome_24h jsonb,
  reward numeric,
  created_at timestamptz not null default now()
);
create index if not exists idx_experience_replay_mode_ts on experience_replay(mode, ts desc);
create index if not exists idx_experience_replay_symbol_ts on experience_replay(symbol, ts desc);

create table if not exists smartbrain_models (
  id uuid primary key default gen_random_uuid(),
  version text not null,
  model_type text not null,
  storage_path text not null,
  metrics jsonb,
  is_active boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists smartbrain_settings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  enabled boolean not null default false,
  paper_mode boolean not null default true,
  risk_level text not null default 'medium',
  max_leverage int not null default 3,
  max_portfolio_risk numeric not null default 0.02,
  max_drawdown_circuit numeric not null default 0.08,
  assets_whitelist text[] not null default '{BTC,ETH}',
  emergency_stop boolean not null default false,
  updated_at timestamptz not null default now()
);

create table if not exists smartbrain_performance (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  mode text not null check (mode in ('paper', 'live')),
  sharpe numeric,
  profit_factor numeric,
  max_drawdown numeric,
  win_rate numeric,
  model_version text,
  created_at timestamptz not null default now()
);

create table if not exists smartbrain_equity_curve (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null,
  mode text not null check (mode in ('paper', 'live')),
  equity numeric not null,
  unrealized_pnl numeric,
  meta jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_smartbrain_equity_curve_mode_ts on smartbrain_equity_curve(mode, ts desc);

create table if not exists smartbrain_logs (
  id uuid primary key default gen_random_uuid(),
  level text not null,
  message text not null,
  context jsonb,
  created_at timestamptz not null default now()
);
