use std::{
    collections::HashMap,
    env,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

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

#[derive(Debug, Clone, Serialize)]
struct HealthResponse {
    ok: bool,
    service: &'static str,
}

#[derive(Debug, Clone)]
struct CachedSnapshot {
    snapshot: OrderbookSnapshot,
    fetched_at: Instant,
}

#[derive(Clone)]
struct AppState {
    http: Client,
    polymarket_host: String,
    cache_ttl: Duration,
    cache: Arc<RwLock<HashMap<String, CachedSnapshot>>>,
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let port = env::var("EXECUTION_SIDECAR_PORT")
        .ok()
        .and_then(|raw| raw.parse::<u16>().ok())
        .unwrap_or(8787);
    let polymarket_host = env::var("POLYMARKET_HOST").unwrap_or_else(|_| "https://clob.polymarket.com".to_string());
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
        cache_ttl,
        cache: Arc::new(RwLock::new(HashMap::new())),
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/v1/market/snapshot/{token_id}", get(fetch_snapshot))
        .route("/v1/market/prime", post(prime_market))
        .route("/v1/execute/plan", post(build_execution_plan))
        .route("/v1/fills/reconcile", post(reconcile_fills))
        .with_state(state);

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
            },
        );
    }
    Ok(snapshot)
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
}
