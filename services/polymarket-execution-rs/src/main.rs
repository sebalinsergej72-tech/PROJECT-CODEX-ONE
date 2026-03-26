use std::{
    collections::{HashMap, HashSet},
    env,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::URL_SAFE, Engine as _};
use chrono::{DateTime, Utc};
use ethers_signers::{LocalWallet, Signer};
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Sha256;
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::TcpStream,
};
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct OrderbookLevel {
    price: f64,
    size: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct OrderbookSnapshot {
    bids: Vec<OrderbookLevel>,
    asks: Vec<OrderbookLevel>,
    best_bid: Option<f64>,
    best_ask: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PrimeMarketDataRequest {
    token_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PrimeMarketDataResponse {
    snapshots: HashMap<String, Option<OrderbookSnapshot>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ExecutableSnapshot {
    best_bid: Option<f64>,
    best_ask: Option<f64>,
    buy_vwap_5usd: Option<f64>,
    buy_vwap_10usd: Option<f64>,
    buy_vwap_25usd: Option<f64>,
    sell_vwap_5usd: Option<f64>,
    sell_vwap_10usd: Option<f64>,
    sell_vwap_25usd: Option<f64>,
    top5_ask_liquidity_usd: f64,
    top5_bid_liquidity_usd: f64,
    has_book: bool,
    last_update_ts: Option<f64>,
    snapshot_age_ms: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PrimeExecutableMarketDataResponse {
    snapshots: HashMap<String, Option<ExecutableSnapshot>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RegisterHotMarketsRequest {
    token_ids: Vec<String>,
    ttl_seconds: Option<u64>,
    source: Option<String>,
    priority: Option<i32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RegisterHotMarketsResponse {
    accepted: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WalletScanTarget {
    wallet_address: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WalletSignalRow {
    external_trade_id: String,
    wallet_address: String,
    market_id: String,
    token_id: Option<String>,
    outcome: String,
    side: String,
    price_cents: f64,
    size_usd: f64,
    traded_at_ts: f64,
    profit_usd: Option<f64>,
    market_slug: Option<String>,
    market_category: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct HotWalletScanRequest {
    wallets: Vec<WalletScanTarget>,
    signal_limit: usize,
    hot_market_ttl_seconds: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct HotWalletScanResponse {
    signals: HashMap<String, Vec<WalletSignalRow>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SidecarOrderRequest {
    token_id: String,
    side: String,
    price_cents: f64,
    size_usd: f64,
    market_id: String,
    outcome: String,
    order_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ExecutionPlanRequest {
    external_trade_id: String,
    source_price_cents: f64,
    target_size_usd: f64,
    risk_mode: String,
    max_slippage_bps: f64,
    max_allowed_slippage_bps: f64,
    min_valid_price: f64,
    min_orderbook_liquidity_usd: Option<f64>,
    liquidity_buffer_multiplier: Option<f64>,
    max_price_deviation_pct: Option<f64>,
    min_absolute_price_deviation_cents: Option<f64>,
    orderbook: Option<OrderbookSnapshot>,
    order: SidecarOrderRequest,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ExecutionPlanResponse {
    allowed: bool,
    reason: Option<String>,
    requested_price_cents: Option<f64>,
    requested_slippage_bps: Option<f64>,
    order_type: Option<String>,
    echoed_order: Option<SidecarOrderRequest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FillRow {
    order_id: Option<String>,
    market_id: Option<String>,
    token_id: Option<String>,
    side: String,
    price_cents: f64,
    size_shares: f64,
    size_usd: f64,
    traded_at_ts: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FillReconcileRequest {
    copied_trade_size_usd: f64,
    current_filled_quantity: f64,
    current_filled_size_usd: f64,
    fills: Vec<FillRow>,
    order_open: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FillReconcileResponse {
    status: String,
    reason: String,
    total_quantity: f64,
    total_size_usd: f64,
    filled_price_cents: f64,
    delta_quantity: f64,
    delta_size_usd: f64,
    delta_price_cents: f64,
    latest_fill_ts: Option<f64>,
    order_open: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PushSubscribeRequest {
    #[serde(rename = "type")]
    message_type: String,
    token_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PushSnapshotMessage {
    #[serde(rename = "type")]
    message_type: String,
    token_id: String,
    snapshot: Option<ExecutableSnapshot>,
}

#[derive(Debug, Clone, Deserialize)]
struct OpenOrdersQuery {
    market_id: Option<String>,
    token_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct AuthenticatedRowsResponse {
    rows: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize)]
struct AccountFillsQuery {
    after_ts: f64,
    market_id: Option<String>,
    token_id: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize)]
struct AuthenticatedOrderStatusResponse {
    payload: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
struct HealthResponse {
    ok: bool,
    service: &'static str,
}

#[derive(Debug, Clone)]
struct CachedSnapshot {
    snapshot: OrderbookSnapshot,
    fetched_at: Instant,
    fetched_at_ts: f64,
}

#[allow(dead_code)]
#[derive(Debug, Clone)]
struct HotMarketEntry {
    source: String,
    priority: i32,
    registered_at_ts: f64,
    expires_at: Instant,
}

#[derive(Debug, Clone)]
struct AuthState {
    signer_address: Option<String>,
    api_key: Option<String>,
    api_secret: Option<String>,
    api_passphrase: Option<String>,
}

#[derive(Clone)]
struct AppState {
    http: Client,
    polymarket_host: String,
    polymarket_data_api_host: String,
    cache_ttl: Duration,
    cache: Arc<RwLock<HashMap<String, CachedSnapshot>>>,
    hot_registry: Arc<RwLock<HashMap<String, HotMarketEntry>>>,
    auth: AuthState,
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let port = env::var("EXECUTION_SIDECAR_PORT")
        .ok()
        .and_then(|raw| raw.parse::<u16>().ok())
        .unwrap_or(8787);
    let push_port = env::var("EXECUTION_SIDECAR_PUSH_PORT")
        .ok()
        .and_then(|raw| raw.parse::<u16>().ok())
        .unwrap_or(8788);
    let push_interval_ms = env::var("EXECUTION_SIDECAR_PUSH_INTERVAL_MS")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .unwrap_or(150);
    let polymarket_host = env::var("POLYMARKET_HOST").unwrap_or_else(|_| "https://clob.polymarket.com".to_string());
    let polymarket_data_api_host =
        env::var("POLYMARKET_DATA_API_HOST").unwrap_or_else(|_| "https://data-api.polymarket.com".to_string());
    let signer_address = env::var("POLYMARKET_SIGNER_ADDRESS")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            env::var("POLYMARKET_PRIVATE_KEY")
                .ok()
                .and_then(|raw| parse_signer_address(&raw))
        });
    let cache_ttl = Duration::from_secs(
        env::var("EXECUTION_SIDECAR_CACHE_TTL_SECONDS")
            .ok()
            .and_then(|raw| raw.parse::<u64>().ok())
            .unwrap_or(10),
    );

    let state = AppState {
        http: Client::builder()
            .use_rustls_tls()
            .build()
            .context("failed to build reqwest client")?,
        polymarket_host,
        polymarket_data_api_host,
        cache_ttl,
        cache: Arc::new(RwLock::new(HashMap::new())),
        hot_registry: Arc::new(RwLock::new(HashMap::new())),
        auth: AuthState {
            signer_address,
            api_key: env::var("POLYMARKET_API_KEY").ok().filter(|value| !value.trim().is_empty()),
            api_secret: env::var("POLYMARKET_API_SECRET").ok().filter(|value| !value.trim().is_empty()),
            api_passphrase: env::var("POLYMARKET_API_PASSPHRASE").ok().filter(|value| !value.trim().is_empty()),
        },
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/market/snapshot/{token_id}", get(fetch_snapshot))
        .route("/v1/market/prime", post(prime_market))
        .route("/v1/market/register_hot", post(register_hot_markets))
        .route("/v1/market/executable/{token_id}", get(fetch_executable_snapshot))
        .route("/v1/market/executable/prime", post(prime_executable_market))
        .route("/v1/orders/open", get(fetch_authenticated_open_orders))
        .route("/v1/orders/{order_id}/status", get(fetch_authenticated_order_status))
        .route("/v1/fills/account", get(fetch_authenticated_account_fills))
        .route("/v1/signals/hot_scan", post(scan_hot_wallets))
        .route("/v1/execute/plan", post(build_execution_plan))
        .route("/v1/fills/reconcile", post(reconcile_fills))
        .with_state(state.clone());

    let push_state = state.clone();
    tokio::spawn(async move {
        if let Err(err) = run_push_server(push_state, push_port, Duration::from_millis(push_interval_ms)).await {
            warn!("push server exited: {:?}", err);
        }
    });

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .with_context(|| format!("failed to bind sidecar on port {}", port))?;
    info!("polymarket-execution-rs listening on http://127.0.0.1:{}", port);
    axum::serve(listener, app).await.context("axum server failed")
}

fn init_tracing() {
    let env_filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(env_filter).init();
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        ok: true,
        service: "polymarket-execution-rs",
    })
}

async fn fetch_snapshot(
    State(state): State<AppState>,
    Path(token_id): Path<String>,
) -> impl IntoResponse {
    match get_or_fetch_snapshot(&state, &token_id).await {
        Ok(Some(snapshot)) => (StatusCode::OK, Json(snapshot)).into_response(),
        Ok(None) => StatusCode::NOT_FOUND.into_response(),
        Err(err) => {
            warn!("snapshot fetch failed for {}: {:?}", token_id, err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn prime_market(
    State(state): State<AppState>,
    Json(request): Json<PrimeMarketDataRequest>,
) -> Json<PrimeMarketDataResponse> {
    let mut snapshots = HashMap::new();
    for token_id in request.token_ids {
        match get_or_fetch_snapshot(&state, &token_id).await {
            Ok(snapshot) => {
                snapshots.insert(token_id, snapshot);
            }
            Err(err) => {
                warn!("prime fetch failed: {:?}", err);
                snapshots.insert(token_id, None);
            }
        }
    }
    Json(PrimeMarketDataResponse { snapshots })
}

async fn register_hot_markets(
    State(state): State<AppState>,
    Json(request): Json<RegisterHotMarketsRequest>,
) -> Json<RegisterHotMarketsResponse> {
    let accepted = register_hot_tokens(
        &state,
        &request.token_ids,
        request.ttl_seconds.unwrap_or(300),
        request.source.as_deref().unwrap_or("api"),
        request.priority.unwrap_or(0),
    )
    .await;
    Json(RegisterHotMarketsResponse { accepted })
}

async fn fetch_executable_snapshot(
    State(state): State<AppState>,
    Path(token_id): Path<String>,
) -> impl IntoResponse {
    match get_or_fetch_executable_snapshot(&state, &token_id).await {
        Ok(Some(snapshot)) => (StatusCode::OK, Json(snapshot)).into_response(),
        Ok(None) => StatusCode::NOT_FOUND.into_response(),
        Err(err) => {
            warn!("executable snapshot fetch failed for {}: {:?}", token_id, err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn prime_executable_market(
    State(state): State<AppState>,
    Json(request): Json<PrimeMarketDataRequest>,
) -> Json<PrimeExecutableMarketDataResponse> {
    let mut snapshots = HashMap::new();
    for token_id in request.token_ids {
        match get_or_fetch_executable_snapshot(&state, &token_id).await {
            Ok(snapshot) => {
                snapshots.insert(token_id, snapshot);
            }
            Err(err) => {
                warn!("prime executable fetch failed: {:?}", err);
                snapshots.insert(token_id, None);
            }
        }
    }
    Json(PrimeExecutableMarketDataResponse { snapshots })
}

async fn fetch_authenticated_open_orders(
    State(state): State<AppState>,
    Query(query): Query<OpenOrdersQuery>,
) -> impl IntoResponse {
    match fetch_open_orders_via_auth(&state, query.market_id.as_deref(), query.token_id.as_deref()).await {
        Ok(rows) => (StatusCode::OK, Json(AuthenticatedRowsResponse { rows })).into_response(),
        Err(err) => {
            warn!("authenticated open orders fetch failed: {:?}", err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn fetch_authenticated_order_status(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
) -> impl IntoResponse {
    match fetch_order_status_via_auth(&state, &order_id).await {
        Ok(payload) => (StatusCode::OK, Json(AuthenticatedOrderStatusResponse { payload })).into_response(),
        Err(err) => {
            warn!("authenticated order status fetch failed for {}: {:?}", order_id, err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn fetch_authenticated_account_fills(
    State(state): State<AppState>,
    Query(query): Query<AccountFillsQuery>,
) -> impl IntoResponse {
    match fetch_account_fills_via_auth(
        &state,
        query.after_ts,
        query.market_id.as_deref(),
        query.token_id.as_deref(),
        query.limit.unwrap_or(200),
    )
    .await
    {
        Ok(rows) => (StatusCode::OK, Json(AuthenticatedRowsResponse { rows })).into_response(),
        Err(err) => {
            warn!("authenticated account fills fetch failed: {:?}", err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn scan_hot_wallets(
    State(state): State<AppState>,
    Json(request): Json<HotWalletScanRequest>,
) -> impl IntoResponse {
    match scan_hot_wallet_signals(&state, request).await {
        Ok(response) => (StatusCode::OK, Json(response)).into_response(),
        Err(err) => {
            warn!("hot wallet scan failed: {:?}", err);
            StatusCode::BAD_GATEWAY.into_response()
        }
    }
}

async fn build_execution_plan(
    Json(request): Json<ExecutionPlanRequest>,
) -> Json<ExecutionPlanResponse> {
    Json(build_execution_plan_response(request))
}

async fn reconcile_fills(
    Json(request): Json<FillReconcileRequest>,
) -> Json<FillReconcileResponse> {
    Json(build_fill_reconcile_response(request))
}

async fn run_push_server(state: AppState, port: u16, interval: Duration) -> Result<()> {
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .with_context(|| format!("failed to bind push server on port {}", port))?;
    info!("polymarket-execution-rs push stream listening on tcp://127.0.0.1:{}", port);
    loop {
        let (stream, peer) = listener.accept().await.context("push accept failed")?;
        let state = state.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_push_client(state, stream, interval).await {
                warn!("push client {} failed: {:?}", peer, err);
            }
        });
    }
}

async fn handle_push_client(state: AppState, stream: TcpStream, interval: Duration) -> Result<()> {
    let (read_half, mut write_half) = stream.into_split();
    let mut lines = BufReader::new(read_half).lines();
    let mut ticker = tokio::time::interval(interval);
    let mut subscribed = HashSet::<String>::new();
    let mut last_sent_version = HashMap::<String, Option<f64>>::new();

    loop {
        tokio::select! {
            maybe_line = lines.next_line() => {
                match maybe_line.context("failed reading push line")? {
                    Some(line) => {
                        if let Some(next_tokens) = parse_subscribe_command(&line) {
                            subscribed = next_tokens;
                            last_sent_version.retain(|token_id, _| subscribed.contains(token_id));
                            push_current_snapshots(&state, &subscribed, &mut last_sent_version, &mut write_half).await?;
                        }
                    }
                    None => break,
                }
            }
            _ = ticker.tick(), if !subscribed.is_empty() => {
                push_current_snapshots(&state, &subscribed, &mut last_sent_version, &mut write_half).await?;
            }
        }
    }

    Ok(())
}

fn parse_subscribe_command(line: &str) -> Option<HashSet<String>> {
    let command = serde_json::from_str::<PushSubscribeRequest>(line).ok()?;
    if command.message_type.to_lowercase() != "subscribe" {
        return None;
    }
    Some(
        command
            .token_ids
            .into_iter()
            .map(|token_id| token_id.trim().to_string())
            .filter(|token_id| !token_id.is_empty())
            .collect(),
    )
}

async fn push_current_snapshots(
    state: &AppState,
    subscribed: &HashSet<String>,
    last_sent_version: &mut HashMap<String, Option<f64>>,
    writer: &mut tokio::net::tcp::OwnedWriteHalf,
) -> Result<()> {
    let mut token_ids = subscribed.iter().cloned().collect::<Vec<_>>();
    token_ids.sort();
    for token_id in token_ids {
        if !is_token_hot(state, &token_id).await {
            continue;
        }
        let snapshot = get_or_fetch_executable_snapshot(state, &token_id).await?;
        let version = snapshot.as_ref().and_then(|item| item.last_update_ts);
        let already_sent = last_sent_version.get(&token_id).copied();
        if already_sent == Some(version) {
            continue;
        }
        let message = PushSnapshotMessage {
            message_type: "snapshot".to_string(),
            token_id: token_id.clone(),
            snapshot,
        };
        let payload = serde_json::to_string(&message).context("failed to serialize push snapshot")?;
        writer.write_all(payload.as_bytes()).await.context("failed writing push payload")?;
        writer.write_all(b"\n").await.context("failed writing push newline")?;
        last_sent_version.insert(token_id, version);
    }
    writer.flush().await.context("failed to flush push stream")
}

async fn is_token_hot(state: &AppState, token_id: &str) -> bool {
    let mut registry = state.hot_registry.write().await;
    registry.retain(|_, entry| entry.expires_at > Instant::now());
    registry.contains_key(token_id)
}

async fn register_hot_tokens(
    state: &AppState,
    token_ids: &[String],
    ttl_seconds: u64,
    source: &str,
    priority: i32,
) -> usize {
    let ttl = Duration::from_secs(ttl_seconds.max(1));
    let now_ts = unix_ts_now();
    let mut accepted = 0_usize;
    let mut registry = state.hot_registry.write().await;
    registry.retain(|_, entry| entry.expires_at > Instant::now());
    for token_id in token_ids.iter().map(|value| value.trim()).filter(|value| !value.is_empty()) {
        registry.insert(
            token_id.to_string(),
            HotMarketEntry {
                source: source.to_string(),
                priority,
                registered_at_ts: now_ts,
                expires_at: Instant::now() + ttl,
            },
        );
        accepted += 1;
    }
    accepted
}

async fn scan_hot_wallet_signals(state: &AppState, request: HotWalletScanRequest) -> Result<HotWalletScanResponse> {
    let wallets = request
        .wallets
        .into_iter()
        .map(|wallet| wallet.wallet_address.trim().to_string())
        .filter(|wallet| !wallet.is_empty())
        .collect::<Vec<_>>();
    let mut handles = Vec::new();
    let signal_limit = request.signal_limit.max(1);
    let hot_market_ttl_seconds = request.hot_market_ttl_seconds.unwrap_or(300);
    for wallet in wallets {
        let state = state.clone();
        handles.push(tokio::spawn(async move {
            let rows = fetch_wallet_trade_signals(&state, &wallet, signal_limit).await;
            (wallet, rows)
        }));
    }

    let mut signals = HashMap::new();
    let mut hot_tokens = Vec::new();
    for handle in handles {
        let (wallet, rows) = handle.await.context("hot wallet task join failed")?;
        let rows = rows?;
        for row in &rows {
            if let Some(token_id) = row.token_id.as_ref() {
                if !token_id.trim().is_empty() {
                    hot_tokens.push(token_id.trim().to_string());
                }
            }
        }
        signals.insert(wallet.to_lowercase(), rows);
    }
    let _ = register_hot_tokens(
        state,
        &hot_tokens,
        hot_market_ttl_seconds,
        "hot_wallet_scan",
        200,
    )
    .await;
    Ok(HotWalletScanResponse { signals })
}

async fn fetch_wallet_trade_signals(
    state: &AppState,
    wallet_address: &str,
    limit: usize,
) -> Result<Vec<WalletSignalRow>> {
    let normalized_wallet = wallet_address.trim().to_lowercase();
    if normalized_wallet.is_empty() {
        return Ok(Vec::new());
    }
    let variants = [
        ("user", wallet_address.to_string()),
        ("address", wallet_address.to_string()),
        ("walletAddress", wallet_address.to_string()),
    ];
    for (param_name, param_value) in variants {
        let url = format!("{}/v1/trades", state.polymarket_data_api_host.trim_end_matches('/'));
        let response = state
            .http
            .get(&url)
            .query(&[(param_name, param_value.as_str()), ("limit", &limit.to_string())])
            .send()
            .await;
        let Ok(response) = response else {
            continue;
        };
        if !response.status().is_success() {
            continue;
        }
        let payload: Value = response.json().await.context("wallet trades json parse failed")?;
        let rows = coerce_rows(&payload);
        let mut filtered = rows
            .into_iter()
            .filter(|row| wallet_matches(row, &normalized_wallet))
            .filter_map(parse_wallet_signal_row)
            .collect::<Vec<_>>();
        filtered.sort_by(|left, right| {
            right
                .traded_at_ts
                .partial_cmp(&left.traded_at_ts)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        filtered.truncate(limit);
        if !filtered.is_empty() {
            return Ok(filtered);
        }
    }
    Ok(Vec::new())
}

fn coerce_rows(payload: &Value) -> Vec<Value> {
    match payload {
        Value::Array(rows) => rows.clone(),
        Value::Object(map) => {
            for key in ["rows", "data", "trades", "activity", "items"] {
                if let Some(Value::Array(rows)) = map.get(key) {
                    return rows.clone();
                }
            }
            Vec::new()
        }
        _ => Vec::new(),
    }
}

fn wallet_matches(row: &Value, normalized_wallet: &str) -> bool {
    let Some(object) = row.as_object() else {
        return false;
    };
    for key in ["user", "address", "walletAddress", "wallet_address", "proxyWallet"] {
        if let Some(Value::String(value)) = object.get(key) {
            if value.trim().to_lowercase() == normalized_wallet {
                return true;
            }
        }
    }
    true
}

fn parse_wallet_signal_row(row: Value) -> Option<WalletSignalRow> {
    let object = row.as_object()?;
    let trade_id = get_string(object, &["id", "tradeID", "tradeId"]).unwrap_or_else(|| {
        format!(
            "{}:{}:{}:{}:{}:{}",
            get_string(object, &["transactionHash"]).unwrap_or_default(),
            get_string(object, &["asset"]).unwrap_or_default(),
            get_string(object, &["conditionId"]).unwrap_or_default(),
            stringify_value(object.get("timestamp")),
            get_string(object, &["side"]).unwrap_or_default(),
            stringify_value(object.get("size")),
        )
    });
    let market_id = get_string(object, &["market", "marketId", "conditionId"])?;
    let token_id = get_string(object, &["tokenID", "tokenId", "asset"]);
    let outcome = get_string(object, &["outcome", "title"]).unwrap_or_else(|| "UNKNOWN".to_string());
    let side = get_string(object, &["side", "type"]).unwrap_or_else(|| "buy".to_string()).to_lowercase();
    let raw_price = get_float(object, &["price", "pricePaid", "avgPrice"])?;
    let raw_size = get_float(object, &["size", "amount", "usdcValue", "notional", "sizeUsd"])?;
    if raw_price <= 0.0 || raw_size <= 0.0 {
        return None;
    }
    let traded_at_ts = parse_timestamp_to_unix(object.get("timestamp").or_else(|| object.get("createdAt")).or_else(|| object.get("time")))?;
    Some(WalletSignalRow {
        external_trade_id: trade_id,
        wallet_address: get_string(object, &["user", "address", "walletAddress", "wallet_address"]).unwrap_or_default(),
        market_id,
        token_id,
        outcome,
        side,
        price_cents: ensure_price_cents(raw_price),
        size_usd: raw_size,
        traded_at_ts,
        profit_usd: get_optional_float(object, &["pnl", "profit", "realizedPnl"]),
        market_slug: get_string(object, &["marketSlug", "market_slug", "slug"]),
        market_category: get_string(object, &["marketCategory", "category"]),
    })
}

fn get_string(object: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(Value::String(value)) = object.get(*key) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_string());
            }
        }
    }
    None
}

fn get_float(object: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<f64> {
    for key in keys {
        if let Some(value) = object.get(*key) {
            if let Some(parsed) = parse_float(Some(value)) {
                return Some(parsed);
            }
        }
    }
    None
}

fn get_optional_float(object: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<f64> {
    get_float(object, keys)
}

fn parse_timestamp_to_unix(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().map(normalize_unix_ts),
        Value::String(text) => {
            let trimmed = text.trim();
            if let Ok(parsed) = trimmed.parse::<f64>() {
                return Some(normalize_unix_ts(parsed));
            }
            DateTime::parse_from_rfc3339(trimmed)
                .ok()
                .map(|dt| dt.with_timezone(&Utc).timestamp_millis() as f64 / 1000.0)
        }
        _ => None,
    }
}

fn normalize_unix_ts(value: f64) -> f64 {
    if value > 10_000_000_000.0 {
        value / 1000.0
    } else {
        value
    }
}

fn ensure_price_cents(raw_price: f64) -> f64 {
    if raw_price <= 1.0 {
        round4(raw_price * 100.0)
    } else {
        round4(raw_price)
    }
}

fn stringify_value(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(item)) => item.clone(),
        Some(other) => other.to_string(),
        None => String::new(),
    }
}

fn parse_signer_address(private_key: &str) -> Option<String> {
    private_key
        .trim()
        .parse::<LocalWallet>()
        .ok()
        .map(|wallet| format!("{:#x}", wallet.address()))
}

fn build_level2_headers(
    state: &AppState,
    method: &str,
    request_path: &str,
    body: Option<&str>,
) -> Result<reqwest::header::HeaderMap> {
    let signer_address = state
        .auth
        .signer_address
        .as_ref()
        .context("missing POLYMARKET_SIGNER_ADDRESS / POLYMARKET_PRIVATE_KEY for sidecar authenticated reads")?;
    let api_key = state
        .auth
        .api_key
        .as_ref()
        .context("missing POLYMARKET_API_KEY for sidecar authenticated reads")?;
    let api_secret = state
        .auth
        .api_secret
        .as_ref()
        .context("missing POLYMARKET_API_SECRET for sidecar authenticated reads")?;
    let api_passphrase = state
        .auth
        .api_passphrase
        .as_ref()
        .context("missing POLYMARKET_API_PASSPHRASE for sidecar authenticated reads")?;

    let timestamp = unix_ts_now().floor() as i64;
    let mut message = format!("{}{}{}", timestamp, method, request_path);
    if let Some(serialized_body) = body {
        message.push_str(serialized_body);
    }

    let secret = URL_SAFE
        .decode(api_secret.as_bytes())
        .context("invalid base64 POLYMARKET_API_SECRET")?;
    let mut mac = HmacSha256::new_from_slice(&secret).context("failed to initialize HMAC")?;
    mac.update(message.as_bytes());
    let signature = URL_SAFE.encode(mac.finalize().into_bytes());

    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert("POLY_ADDRESS", signer_address.parse()?);
    headers.insert("POLY_SIGNATURE", signature.parse()?);
    headers.insert("POLY_TIMESTAMP", timestamp.to_string().parse()?);
    headers.insert("POLY_API_KEY", api_key.parse()?);
    headers.insert("POLY_PASSPHRASE", api_passphrase.parse()?);
    headers.insert(reqwest::header::USER_AGENT, "polymarket-execution-rs".parse()?);
    headers.insert(reqwest::header::ACCEPT, "*/*".parse()?);
    headers.insert(reqwest::header::CONNECTION, "keep-alive".parse()?);
    headers.insert(reqwest::header::CONTENT_TYPE, "application/json".parse()?);
    headers.insert(reqwest::header::ACCEPT_ENCODING, "gzip".parse()?);
    Ok(headers)
}

async fn fetch_open_orders_via_auth(
    state: &AppState,
    market_id: Option<&str>,
    token_id: Option<&str>,
) -> Result<Vec<Value>> {
    let headers = build_level2_headers(state, "GET", "/data/orders", None)?;
    let mut rows = Vec::new();
    let mut next_cursor = "MA==".to_string();
    loop {
        let mut request = state
            .http
            .get(format!("{}/data/orders", state.polymarket_host.trim_end_matches('/')))
            .headers(headers.clone())
            .query(&[("next_cursor", next_cursor.as_str())]);
        if let Some(market_id) = market_id.filter(|value| !value.trim().is_empty()) {
            request = request.query(&[("market", market_id)]);
        }
        if let Some(token_id) = token_id.filter(|value| !value.trim().is_empty()) {
            request = request.query(&[("asset_id", token_id)]);
        }
        let response = request.send().await.context("open orders request failed")?;
        if !response.status().is_success() {
            anyhow::bail!("open orders request returned {}", response.status());
        }
        let payload: Value = response.json().await.context("open orders json parse failed")?;
        if let Some(data) = payload.get("data").and_then(Value::as_array) {
            rows.extend(data.iter().cloned());
        }
        let Some(cursor) = payload.get("next_cursor").and_then(Value::as_str) else {
            break;
        };
        if cursor == "LTE=" || cursor == next_cursor {
            break;
        }
        next_cursor = cursor.to_string();
    }
    Ok(rows)
}

async fn fetch_account_fills_via_auth(
    state: &AppState,
    after_ts: f64,
    market_id: Option<&str>,
    token_id: Option<&str>,
    limit: usize,
) -> Result<Vec<Value>> {
    let headers = build_level2_headers(state, "GET", "/data/trades", None)?;
    let mut rows = Vec::new();
    let mut next_cursor = "MA==".to_string();
    let safe_limit = limit.clamp(1, 500);
    loop {
        let after_string = format!("{}", after_ts.floor() as i64);
        let mut request = state
            .http
            .get(format!("{}/data/trades", state.polymarket_host.trim_end_matches('/')))
            .headers(headers.clone())
            .query(&[("next_cursor", next_cursor.as_str()), ("after", after_string.as_str())]);
        if let Some(market_id) = market_id.filter(|value| !value.trim().is_empty()) {
            request = request.query(&[("market", market_id)]);
        }
        if let Some(token_id) = token_id.filter(|value| !value.trim().is_empty()) {
            request = request.query(&[("asset_id", token_id)]);
        }
        let response = request.send().await.context("account fills request failed")?;
        if !response.status().is_success() {
            anyhow::bail!("account fills request returned {}", response.status());
        }
        let payload: Value = response.json().await.context("account fills json parse failed")?;
        if let Some(data) = payload.get("data").and_then(Value::as_array) {
            rows.extend(data.iter().cloned());
            if rows.len() >= safe_limit {
                rows.truncate(safe_limit);
                break;
            }
        }
        let Some(cursor) = payload.get("next_cursor").and_then(Value::as_str) else {
            break;
        };
        if cursor == "LTE=" || cursor == next_cursor {
            break;
        }
        next_cursor = cursor.to_string();
    }
    Ok(rows)
}

async fn fetch_order_status_via_auth(state: &AppState, order_id: &str) -> Result<Option<Value>> {
    let normalized = order_id.trim();
    if normalized.is_empty() {
        return Ok(None);
    }
    let request_path = format!("/data/order/{normalized}");
    let headers = build_level2_headers(state, "GET", &request_path, None)?;
    let response = state
        .http
        .get(format!("{}{}", state.polymarket_host.trim_end_matches('/'), request_path))
        .headers(headers)
        .send()
        .await
        .context("order status request failed")?;
    if response.status() == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    if !response.status().is_success() {
        anyhow::bail!("order status request returned {}", response.status());
    }
    let payload: Value = response.json().await.context("order status json parse failed")?;
    Ok(Some(payload))
}

fn build_execution_plan_response(request: ExecutionPlanRequest) -> ExecutionPlanResponse {
    if let Some(reason) = evaluate_tradability(&request) {
        return ExecutionPlanResponse {
            allowed: false,
            reason: Some(reason),
            requested_price_cents: None,
            requested_slippage_bps: None,
            order_type: None,
            echoed_order: None,
        };
    }

    let side = request.order.side.to_lowercase();
    let risk_mode = request.risk_mode.to_lowercase();
    let mut requested_price_cents = request.source_price_cents.max(0.0001);
    let mut requested_slippage_bps = 0.0_f64;
    let mut order_type = request.order.order_type.clone();

    if risk_mode == "aggressive" {
        requested_slippage_bps = request.max_slippage_bps.max(0.0);
        if side == "buy" {
            requested_price_cents = round4(request.source_price_cents * (1.0 + requested_slippage_bps / 10_000.0));
            order_type = "GTC".to_string();
        } else {
            requested_price_cents = round4(request.source_price_cents * (1.0 - requested_slippage_bps / 10_000.0));
            order_type = "FAK".to_string();
        }

        let actual_slippage = compute_slippage_bps(request.source_price_cents, requested_price_cents, &side);
        if actual_slippage > request.max_allowed_slippage_bps {
            return ExecutionPlanResponse {
                allowed: false,
                reason: Some("slippage_above_hard_limit".to_string()),
                requested_price_cents: None,
                requested_slippage_bps: Some(round4(actual_slippage)),
                order_type: Some(order_type),
                echoed_order: None,
            };
        }
        requested_slippage_bps = round4(actual_slippage);
    }

    let echoed_order = SidecarOrderRequest {
        token_id: request.order.token_id,
        side: request.order.side,
        price_cents: requested_price_cents,
        size_usd: request.target_size_usd,
        market_id: request.order.market_id,
        outcome: request.order.outcome,
        order_type: order_type.clone(),
    };

    ExecutionPlanResponse {
        allowed: true,
        reason: None,
        requested_price_cents: Some(requested_price_cents),
        requested_slippage_bps: Some(requested_slippage_bps),
        order_type: Some(order_type),
        echoed_order: Some(echoed_order),
    }
}

fn evaluate_tradability(request: &ExecutionPlanRequest) -> Option<String> {
    let Some(orderbook) = request.orderbook.as_ref() else {
        return Some("no_orderbook".to_string());
    };
    let side = request.order.side.to_lowercase();
    let levels = if side == "buy" {
        orderbook.asks.iter().take(5)
    } else {
        orderbook.bids.iter().take(5)
    };
    let available_usd: f64 = levels.map(|level| level.price * level.size).sum();
    let required_usd = f64::max(
        request.target_size_usd * request.liquidity_buffer_multiplier.unwrap_or(1.0),
        request.min_orderbook_liquidity_usd.unwrap_or(0.0),
    );
    if available_usd < required_usd {
        return Some(format!("low_liquidity:{available_usd:.2}<{required_usd:.2}"));
    }

    let current_price = if side == "buy" {
        orderbook.best_ask
    } else {
        orderbook.best_bid
    };
    let Some(current_price) = current_price else {
        return Some("no_price_available".to_string());
    };

    let source_price = f64::max(request.source_price_cents / 100.0, request.min_valid_price.max(0.0001));
    let deviation = ((current_price - source_price).abs()) / source_price;
    let absolute_deviation_cents = ((current_price * 100.0) - request.source_price_cents).abs();
    if deviation > request.max_price_deviation_pct.unwrap_or(f64::INFINITY)
        && absolute_deviation_cents > request.min_absolute_price_deviation_cents.unwrap_or(f64::INFINITY)
    {
        return Some(format!("price_moved:{:.1}%", deviation * 100.0));
    }
    None
}

fn build_fill_reconcile_response(request: FillReconcileRequest) -> FillReconcileResponse {
    let mut total_quantity = 0.0_f64;
    let mut total_size_usd = 0.0_f64;
    let mut latest_fill_ts: Option<f64> = None;

    for fill in request.fills {
        total_quantity += fill.size_shares.max(0.0);
        total_size_usd += fill.size_usd.max(0.0);
        latest_fill_ts = match latest_fill_ts {
            Some(existing) if existing >= fill.traded_at_ts => Some(existing),
            _ => Some(fill.traded_at_ts),
        };
    }

    let delta_quantity = (total_quantity - request.current_filled_quantity).max(0.0);
    let delta_size_usd = (total_size_usd - request.current_filled_size_usd).max(0.0);
    let delta_price_cents = if delta_quantity > 0.0 && delta_size_usd > 0.0 {
        round4((delta_size_usd / delta_quantity) * 100.0)
    } else {
        0.0
    };
    let filled_price_cents = if total_quantity > 0.0 {
        round4((total_size_usd / total_quantity) * 100.0)
    } else {
        0.0
    };

    let size_tolerance = f64::max(f64::min(request.copied_trade_size_usd * 0.01, 0.1), 0.01);
    let fully_filled = total_size_usd + size_tolerance >= request.copied_trade_size_usd;

    if total_size_usd <= 0.0 {
        if request.order_open == Some(false) {
            return FillReconcileResponse {
                status: "canceled".to_string(),
                reason: "canceled_without_fill".to_string(),
                total_quantity,
                total_size_usd: round4(total_size_usd),
                filled_price_cents,
                delta_quantity: round4(delta_quantity),
                delta_size_usd: round4(delta_size_usd),
                delta_price_cents,
                latest_fill_ts,
                order_open: request.order_open,
            };
        }
        return FillReconcileResponse {
            status: "submitted".to_string(),
            reason: "submitted_waiting_fill".to_string(),
            total_quantity: round4(total_quantity),
            total_size_usd: round4(total_size_usd),
            filled_price_cents,
            delta_quantity: round4(delta_quantity),
            delta_size_usd: round4(delta_size_usd),
            delta_price_cents,
            latest_fill_ts,
            order_open: request.order_open,
        };
    }

    if fully_filled {
        return FillReconcileResponse {
            status: "filled".to_string(),
            reason: format!("filled @ {filled_price_cents:.2}c"),
            total_quantity: round4(total_quantity),
            total_size_usd: round4(total_size_usd),
            filled_price_cents,
            delta_quantity: round4(delta_quantity),
            delta_size_usd: round4(delta_size_usd),
            delta_price_cents,
            latest_fill_ts,
            order_open: request.order_open,
        };
    }

    let mut reason = format!("partial_fill ${:.2} @ {:.2}c", total_size_usd, filled_price_cents);
    if request.order_open == Some(false) {
        reason.push_str(" | remainder_canceled");
    }
    FillReconcileResponse {
        status: "partial".to_string(),
        reason,
        total_quantity: round4(total_quantity),
        total_size_usd: round4(total_size_usd),
        filled_price_cents,
        delta_quantity: round4(delta_quantity),
        delta_size_usd: round4(delta_size_usd),
        delta_price_cents,
        latest_fill_ts,
        order_open: request.order_open,
    }
}

async fn get_or_fetch_snapshot(state: &AppState, token_id: &str) -> Result<Option<OrderbookSnapshot>> {
    if token_id.trim().is_empty() {
        return Ok(None);
    }

    {
        let cache = state.cache.read().await;
        if let Some(entry) = cache.get(token_id) {
            if entry.fetched_at.elapsed() <= state.cache_ttl {
                debug!("cache hit for token {}", token_id);
                return Ok(Some(entry.snapshot.clone()));
            }
        }
    }

    let snapshot = fetch_snapshot_from_polymarket(&state.http, &state.polymarket_host, token_id).await?;
    if let Some(snapshot) = snapshot.clone() {
        let mut cache = state.cache.write().await;
        cache.insert(
            token_id.to_string(),
            CachedSnapshot {
                snapshot,
                fetched_at: Instant::now(),
                fetched_at_ts: unix_ts_now(),
            },
        );
    }
    Ok(snapshot)
}

async fn get_or_fetch_executable_snapshot(
    state: &AppState,
    token_id: &str,
) -> Result<Option<ExecutableSnapshot>> {
    if token_id.trim().is_empty() {
        return Ok(None);
    }

    {
        let cache = state.cache.read().await;
        if let Some(entry) = cache.get(token_id) {
            if entry.fetched_at.elapsed() <= state.cache_ttl {
                return Ok(Some(build_executable_snapshot(&entry.snapshot, Some(entry.fetched_at_ts), Some(entry.fetched_at.elapsed()))));
            }
        }
    }

    let snapshot = fetch_snapshot_from_polymarket(&state.http, &state.polymarket_host, token_id).await?;
    if let Some(snapshot) = snapshot {
        let fetched_at_ts = unix_ts_now();
        let mut cache = state.cache.write().await;
        cache.insert(
            token_id.to_string(),
            CachedSnapshot {
                snapshot: snapshot.clone(),
                fetched_at: Instant::now(),
                fetched_at_ts,
            },
        );
        return Ok(Some(build_executable_snapshot(&snapshot, Some(fetched_at_ts), Some(Duration::from_millis(0)))));
    }
    Ok(None)
}

async fn fetch_snapshot_from_polymarket(
    http: &Client,
    polymarket_host: &str,
    token_id: &str,
) -> Result<Option<OrderbookSnapshot>> {
    let url = format!("{}/book", polymarket_host.trim_end_matches('/'));
    let response = http.get(url).query(&[("token_id", token_id)]).send().await?;
    if response.status() == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    if !response.status().is_success() {
        return Ok(None);
    }
    let payload: Value = response.json().await?;
    Ok(parse_orderbook_snapshot(&payload))
}

fn parse_orderbook_snapshot(payload: &Value) -> Option<OrderbookSnapshot> {
    let bids = parse_levels(payload.get("bids"))?;
    let asks = parse_levels(payload.get("asks"))?;
    if bids.is_empty() && asks.is_empty() {
        return None;
    }
    let best_bid = bids.first().map(|row| row.price);
    let best_ask = asks.first().map(|row| row.price);
    Some(OrderbookSnapshot {
        bids,
        asks,
        best_bid,
        best_ask,
    })
}

fn parse_levels(value: Option<&Value>) -> Option<Vec<OrderbookLevel>> {
    let rows = value?.as_array()?;
    let mut levels = Vec::new();
    for row in rows {
        let price = parse_float(row.get("price").or_else(|| row.get("p")))?;
        let size = parse_float(
            row.get("size")
                .or_else(|| row.get("quantity"))
                .or_else(|| row.get("shares"))
                .or_else(|| row.get("amount")),
        )?;
        if price > 0.0 && size > 0.0 {
            levels.push(OrderbookLevel { price, size });
        }
    }
    Some(levels)
}

fn parse_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.parse::<f64>().ok(),
        _ => None,
    }
}

fn compute_slippage_bps(source_price_cents: f64, execution_price_cents: f64, side: &str) -> f64 {
    let source = source_price_cents.max(0.0001);
    let execution = execution_price_cents.max(0.0001);
    if side == "buy" {
        ((execution - source) / source).max(0.0) * 10_000.0
    } else {
        ((source - execution) / source).max(0.0) * 10_000.0
    }
}

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

fn build_executable_snapshot(
    snapshot: &OrderbookSnapshot,
    last_update_ts: Option<f64>,
    age: Option<Duration>,
) -> ExecutableSnapshot {
    ExecutableSnapshot {
        best_bid: snapshot.best_bid,
        best_ask: snapshot.best_ask,
        buy_vwap_5usd: compute_vwap_for_notional(&snapshot.asks, 5.0),
        buy_vwap_10usd: compute_vwap_for_notional(&snapshot.asks, 10.0),
        buy_vwap_25usd: compute_vwap_for_notional(&snapshot.asks, 25.0),
        sell_vwap_5usd: compute_vwap_for_notional(&snapshot.bids, 5.0),
        sell_vwap_10usd: compute_vwap_for_notional(&snapshot.bids, 10.0),
        sell_vwap_25usd: compute_vwap_for_notional(&snapshot.bids, 25.0),
        top5_ask_liquidity_usd: round4(snapshot.asks.iter().take(5).map(|level| level.price * level.size).sum()),
        top5_bid_liquidity_usd: round4(snapshot.bids.iter().take(5).map(|level| level.price * level.size).sum()),
        has_book: !(snapshot.bids.is_empty() && snapshot.asks.is_empty()),
        last_update_ts,
        snapshot_age_ms: age.map(|duration| duration.as_millis() as u64),
    }
}

fn compute_vwap_for_notional(levels: &[OrderbookLevel], target_usd: f64) -> Option<f64> {
    let mut remaining_usd = target_usd.max(0.0);
    let mut total_usd = 0.0_f64;
    let mut total_size = 0.0_f64;
    for level in levels {
        if remaining_usd <= 0.0 {
            break;
        }
        let level_usd = level.price * level.size;
        if level.price <= 0.0 || level.size <= 0.0 || level_usd <= 0.0 {
            continue;
        }
        let take_usd = remaining_usd.min(level_usd);
        let take_size = take_usd / level.price;
        total_usd += take_usd;
        total_size += take_size;
        remaining_usd -= take_usd;
    }
    if remaining_usd > 1e-9 || total_size <= 0.0 {
        return None;
    }
    Some(round4(total_usd / total_size))
}

fn unix_ts_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_orderbook_payload_works() {
        let payload = serde_json::json!({
            "bids": [{"price": "0.41", "size": "12"}],
            "asks": [{"price": "0.43", "size": "9"}]
        });
        let snapshot = parse_orderbook_snapshot(&payload).expect("snapshot");
        assert_eq!(snapshot.best_bid, Some(0.41));
        assert_eq!(snapshot.best_ask, Some(0.43));
    }

    #[test]
    fn execution_plan_builds_aggressive_buy() {
        let request = ExecutionPlanRequest {
            external_trade_id: "trade-1".to_string(),
            source_price_cents: 50.0,
            target_size_usd: 7.5,
            risk_mode: "aggressive".to_string(),
            max_slippage_bps: 150.0,
            max_allowed_slippage_bps: 150.0,
            min_valid_price: 0.01,
            min_orderbook_liquidity_usd: Some(10.0),
            liquidity_buffer_multiplier: Some(1.25),
            max_price_deviation_pct: Some(0.08),
            min_absolute_price_deviation_cents: Some(2.0),
            orderbook: Some(OrderbookSnapshot {
                bids: vec![OrderbookLevel { price: 0.49, size: 100.0 }],
                asks: vec![OrderbookLevel { price: 0.50, size: 100.0 }],
                best_bid: Some(0.49),
                best_ask: Some(0.50),
            }),
            order: SidecarOrderRequest {
                token_id: "token-1".to_string(),
                side: "buy".to_string(),
                price_cents: 50.0,
                size_usd: 7.5,
                market_id: "market-1".to_string(),
                outcome: "Yes".to_string(),
                order_type: "GTC".to_string(),
            },
        };
        let response = build_execution_plan_response(request);
        assert!(response.allowed);
        assert_eq!(response.order_type.as_deref(), Some("GTC"));
        assert_eq!(response.requested_price_cents, Some(50.75));
    }

    #[test]
    fn execution_plan_rejects_low_liquidity() {
        let request = ExecutionPlanRequest {
            external_trade_id: "trade-2".to_string(),
            source_price_cents: 60.0,
            target_size_usd: 12.0,
            risk_mode: "aggressive".to_string(),
            max_slippage_bps: 150.0,
            max_allowed_slippage_bps: 150.0,
            min_valid_price: 0.01,
            min_orderbook_liquidity_usd: Some(10.0),
            liquidity_buffer_multiplier: Some(1.25),
            max_price_deviation_pct: Some(0.08),
            min_absolute_price_deviation_cents: Some(2.0),
            orderbook: Some(OrderbookSnapshot {
                bids: vec![OrderbookLevel { price: 0.59, size: 1.0 }],
                asks: vec![OrderbookLevel { price: 0.60, size: 1.0 }],
                best_bid: Some(0.59),
                best_ask: Some(0.60),
            }),
            order: SidecarOrderRequest {
                token_id: "token-2".to_string(),
                side: "buy".to_string(),
                price_cents: 60.0,
                size_usd: 12.0,
                market_id: "market-2".to_string(),
                outcome: "Yes".to_string(),
                order_type: "GTC".to_string(),
            },
        };
        let response = build_execution_plan_response(request);
        assert!(!response.allowed);
        assert_eq!(response.reason.as_deref(), Some("low_liquidity:0.60<15.00"));
    }

    #[test]
    fn fill_reconcile_marks_filled() {
        let response = build_fill_reconcile_response(FillReconcileRequest {
            copied_trade_size_usd: 7.0,
            current_filled_quantity: 0.0,
            current_filled_size_usd: 0.0,
            fills: vec![FillRow {
                order_id: Some("ord-1".to_string()),
                market_id: Some("mkt-1".to_string()),
                token_id: Some("tok-1".to_string()),
                side: "buy".to_string(),
                price_cents: 60.0,
                size_shares: 10.0,
                size_usd: 7.0,
                traded_at_ts: 1_700_000_000.0,
            }],
            order_open: Some(false),
        });
        assert_eq!(response.status, "filled");
        assert_eq!(response.reason, "filled @ 70.00c");
        assert_eq!(response.delta_size_usd, 7.0);
    }

    #[test]
    fn executable_snapshot_computes_vwap_and_liquidity() {
        let snapshot = OrderbookSnapshot {
            bids: vec![
                OrderbookLevel { price: 0.49, size: 20.0 },
                OrderbookLevel { price: 0.48, size: 40.0 },
            ],
            asks: vec![
                OrderbookLevel { price: 0.50, size: 10.0 },
                OrderbookLevel { price: 0.51, size: 20.0 },
            ],
            best_bid: Some(0.49),
            best_ask: Some(0.50),
        };
        let executable = build_executable_snapshot(&snapshot, Some(1_700_000_000.0), Some(Duration::from_millis(25)));
        assert_eq!(executable.buy_vwap_5usd, Some(0.50));
        assert!(executable.buy_vwap_10usd.is_some());
        assert_eq!(executable.top5_ask_liquidity_usd, 15.2);
        assert_eq!(executable.top5_bid_liquidity_usd, 29.0);
        assert_eq!(executable.snapshot_age_ms, Some(25));
    }

    #[test]
    fn parse_subscribe_command_accepts_token_ids() {
        let tokens = parse_subscribe_command(r#"{"type":"subscribe","token_ids":["tok-1"," tok-2 ",""]}"#)
            .expect("tokens");
        assert!(tokens.contains("tok-1"));
        assert!(tokens.contains("tok-2"));
        assert_eq!(tokens.len(), 2);
    }

    #[test]
    fn parse_wallet_signal_row_normalizes_trade_payload() {
        let row = serde_json::json!({
            "id": "trade-1",
            "user": "0xabc",
            "conditionId": "market-1",
            "asset": "token-1",
            "outcome": "Yes",
            "side": "buy",
            "price": "0.61",
            "size": "12.5",
            "timestamp": 1_700_000_000,
            "marketSlug": "sample-market",
            "category": "Politics"
        });
        let signal = parse_wallet_signal_row(row).expect("signal");
        assert_eq!(signal.external_trade_id, "trade-1");
        assert_eq!(signal.market_id, "market-1");
        assert_eq!(signal.token_id.as_deref(), Some("token-1"));
        assert_eq!(signal.price_cents, 61.0);
        assert_eq!(signal.size_usd, 12.5);
        assert_eq!(signal.market_category.as_deref(), Some("Politics"));
    }
}
