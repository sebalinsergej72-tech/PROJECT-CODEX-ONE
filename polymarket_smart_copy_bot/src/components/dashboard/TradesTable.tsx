import { AlertCircle, Info } from "lucide-react";
import { motion } from "framer-motion";

import type { BotStatus, Trade } from "@/lib/api";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  trades: Trade[] | undefined;
  status: BotStatus | undefined;
  isLoading: boolean;
}

function shortAddr(v: string) {
  if (!v) return "-";
  return v.length < 12 ? v : `${v.slice(0, 6)}…${v.slice(-4)}`;
}

function shortId(v: string) {
  if (!v) return "-";
  return v.length <= 12 ? v : `${v.slice(0, 6)}...${v.slice(-4)}`;
}

function money(v: number) {
  return `$${v.toFixed(2)}`;
}

const CATEGORY_COLORS: Record<string, string> = {
  Politics: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  Sports: "bg-green-500/20 text-green-400 border-green-500/30",
  Crypto: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  Science: "bg-purple-500/20 text-purple-400 border-purple-500/30",
  "Pop Culture": "bg-pink-500/20 text-pink-400 border-pink-500/30",
  Pop_Culture: "bg-pink-500/20 text-pink-400 border-pink-500/30",
  Business: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
};

function categoryBadge(category: string | undefined) {
  if (!category) return null;
  const colors = CATEGORY_COLORS[category] || "bg-muted/50 text-muted-foreground border-border";
  return (
    <span className={`mt-0.5 inline-flex items-center rounded-full border px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider ${colors}`}>
      {category.replace(/_/g, " ")}
    </span>
  );
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
                // Map internal statuses to display labels and colors
                const statusLabel =
                  t.status === "filled" || t.status === "executed"
                    ? "EXECUTED"
                    : t.status === "submitted" || t.status === "partial"
                      ? "AWAITING"
                      : (t.status || "-").toUpperCase();
                const statusColor =
                  t.status === "filled" || t.status === "executed"
                    ? "text-profit"
                    : t.status === "submitted" || t.status === "partial"
                      ? "text-warning"
                      : t.status === "failed"
                        ? "text-loss"
                        : t.status === "canceled" || t.status === "expired"
                          ? "text-warning"
                          : "text-muted-foreground";
                return (
                  <tr key={`${t.copied_at}-${t.market_id}-${i}`} className="border-b border-border/30 transition-colors hover:bg-secondary/30">
                    <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">
                      {(t.copied_at || "-").replace("T", " ").slice(0, 19)}
                    </td>
                    <td className={`py-2 pr-3 font-mono text-xs font-bold uppercase ${statusColor}`}>
                      <div className="flex items-center gap-1.5">
                        {statusLabel}
                        {(t.status === "skipped" || t.status === "failed") && t.reason && (
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <div className="cursor-help text-muted-foreground hover:text-foreground transition-colors mt-0.5">
                                  {t.status === "failed" ? <AlertCircle className="h-3 w-3" /> : <Info className="h-3 w-3" />}
                                </div>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="max-w-[250px] font-sans text-xs bg-popover text-popover-foreground border-border break-words">
                                <p>{t.reason}</p>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        )}
                      </div>
                    </td>
                    <td className="max-w-[200px] py-2 pr-3 text-xs" title={t.market_title || t.market_id || ""}>
                      <span className="block truncate">
                        {t.market_title || shortId(t.market_id || "")}
                      </span>
                      {categoryBadge(t.market_category)}
                    </td>
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
