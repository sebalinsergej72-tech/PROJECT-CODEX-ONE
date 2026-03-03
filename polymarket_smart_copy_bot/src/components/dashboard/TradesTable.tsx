import { motion } from "framer-motion";

import type { BotStatus, Trade } from "@/lib/api";

interface Props {
  trades: Trade[] | undefined;
  status: BotStatus | undefined;
  isLoading: boolean;
}

function shortAddr(v: string) {
  if (!v) return "-";
  return v.length < 12 ? v : `${v.slice(0, 6)}…${v.slice(-4)}`;
}

function money(v: number) {
  return `$${v.toFixed(2)}`;
}

export function TradesTable({ trades, status, isLoading }: Props) {
  const liveOnly = status && !status.dry_run && Boolean(status.live_started_at);
  const liveStartedAt = liveOnly && status?.live_started_at ? Date.parse(status.live_started_at) : Number.NaN;
  const filtered = (trades || []).filter((t) => {
    if (!liveOnly) return true;
    const ts = Date.parse(t.copied_at || "");
    return !Number.isNaN(ts) && !Number.isNaN(liveStartedAt) && ts >= liveStartedAt;
  });

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="trading-card overflow-hidden">
      <h3 className="mb-3 text-[11px] font-medium uppercase tracking-widest text-muted-foreground">Recent Trades</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              {["Time", "Status", "Market", "Side", "Size", "Wallet"].map((h) => (
                <th key={h} className="pb-2 pr-3 text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading
              ? Array.from({ length: 3 }).map((_, i) => (
                  <tr key={i} className="border-b border-border/50">
                    {Array.from({ length: 6 }).map((__, j) => (
                      <td key={j} className="py-2 pr-3">
                        <div className="h-4 w-16 animate-pulse rounded bg-muted" />
                      </td>
                    ))}
                  </tr>
                ))
              : null}

            {!isLoading && filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="py-6 text-center text-sm text-muted-foreground">
                  {liveOnly ? "No trades since LIVE mode" : "No trades yet"}
                </td>
              </tr>
            ) : null}

            {!isLoading
              ? filtered.map((t, i) => {
                  const statusColor =
                    t.status === "executed" ? "text-profit" : t.status === "failed" ? "text-loss" : "text-muted-foreground";
                  return (
                    <tr key={`${t.copied_at}-${t.market_id}-${i}`} className="border-b border-border/30 transition-colors hover:bg-secondary/30">
                      <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">
                        {(t.copied_at || "-").replace("T", " ").slice(0, 19)}
                      </td>
                      <td className={`py-2 pr-3 font-mono text-xs font-bold uppercase ${statusColor}`}>{t.status || "-"}</td>
                      <td className="max-w-[120px] truncate py-2 pr-3 font-mono text-xs">{t.market_id || "-"}</td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        {(t.side || "-").toUpperCase()} @ {(t.price_cents || 0).toFixed(1)}¢
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(t.size_usd || 0)}</td>
                      <td className="py-2 font-mono text-xs text-muted-foreground">{shortAddr(t.wallet_address)}</td>
                    </tr>
                  );
                })
              : null}
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}
