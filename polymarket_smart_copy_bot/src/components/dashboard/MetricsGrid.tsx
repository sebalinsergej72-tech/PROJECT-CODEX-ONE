import type { BotStatus } from "@/lib/api";

import { MetricCard } from "./MetricCard";

interface Props {
  status: BotStatus | undefined;
}

function money(v: number) {
  return `$${v.toFixed(2)}`;
}

function moneyNullable(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `$${Number(v).toFixed(2)}`;
}

function pnlTone(v: number): "good" | "bad" | "neutral" {
  if (v > 0) return "good";
  if (v < 0) return "bad";
  return "neutral";
}

export function MetricsGrid({ status }: Props) {
  if (!status) return null;

  const metrics = [
    { label: "Engine", value: status.dry_run ? "DRY RUN" : "LIVE", tone: status.dry_run ? ("warn" as const) : ("good" as const) },
    { label: "Trading", value: status.trading_enabled ? "ENABLED" : "PAUSED", tone: status.trading_enabled ? ("good" as const) : ("warn" as const) },
    { label: "Risk Mode", value: (status.risk_mode || "-").toUpperCase(), tone: "neutral" as const },
    { label: "Wallets Tracked", value: String(status.tracked_wallets ?? "-"), tone: "neutral" as const },
    { label: "Open Positions", value: String(status.open_positions ?? "-"), tone: "neutral" as const },
    { label: "Total Equity", value: money(status.total_equity_usd || 0), tone: "neutral" as const },
    { label: "Exposure", value: money(status.exposure_usd || 0), tone: "neutral" as const },
    { label: "Price Filter", value: status.price_filter_enabled ? "ON" : "OFF", tone: status.price_filter_enabled ? ("good" as const) : ("warn" as const) },
    { label: "Boost", value: status.high_conviction_boost_enabled ? "ON" : "OFF", tone: status.high_conviction_boost_enabled ? ("good" as const) : ("warn" as const) },
    { label: "AutoAdd", value: status.discovery_autoadd ? "ON" : "OFF", tone: status.discovery_autoadd ? ("good" as const) : ("warn" as const) },
    { label: "Daily PnL", value: money(status.daily_pnl_usd || 0), tone: pnlTone(status.daily_pnl_usd || 0) },
    { label: "Cumulative PnL", value: money(status.cumulative_pnl_usd || 0), tone: pnlTone(status.cumulative_pnl_usd || 0) },
    {
      label: "Total Balance (PM)",
      value: moneyNullable(status.account_balances?.total_balance_usd),
      tone: "neutral" as const,
    },
    {
      label: "Positions Value (PM)",
      value: moneyNullable(status.account_balances?.positions_value_usd),
      tone: "neutral" as const,
    },
    {
      label: "Free Balance (PM)",
      value: moneyNullable(status.account_balances?.free_balance_usd),
      tone: "neutral" as const,
    },
    {
      label: "Reserved Orders (PM)",
      value: moneyNullable(status.account_balances?.open_orders_reserved_usd),
      tone: "neutral" as const,
    },
    {
      label: "Net Free (PM)",
      value: moneyNullable(status.account_balances?.net_free_balance_usd),
      tone: "neutral" as const,
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
      {metrics.map((m, i) => (
        <MetricCard key={m.label} label={m.label} value={m.value} tone={m.tone} delay={i} />
      ))}
    </div>
  );
}
