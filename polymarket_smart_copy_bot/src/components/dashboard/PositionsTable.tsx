import { motion } from "framer-motion";
import { useState } from "react";

import type { Position } from "@/lib/api";
import { useClosePosition } from "@/hooks/useBotData";
import { X, Loader2 } from "lucide-react";

interface Props {
  positions: Position[] | undefined;
  isLoading: boolean;
}

function money(v: number) {
  return `$${v.toFixed(2)}`;
}

function pnlClass(v: number) {
  if (v > 0) return "text-profit";
  if (v < 0) return "text-loss";
  return "text-muted-foreground";
}

function cents(v: number) {
  return `${(v / 100).toFixed(2)}`;
}

function shortId(v: string) {
  if (!v) return "-";
  return v.length <= 12 ? v : `${v.slice(0, 6)}...${v.slice(-4)}`;
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

export function PositionsTable({ positions, isLoading }: Props) {
  const { mutate: closePosition, isPending: isClosing } = useClosePosition();
  const [closingId, setClosingId] = useState<number | null>(null);

  const handleClose = (id: number) => {
    setClosingId(id);
    closePosition(id, {
      onSettled: () => setClosingId(null)
    });
  };

  const cols = [
    "Market",
    "Outcome",
    "Side",
    "Qty",
    "Avg Price",
    "Cur Price",
    "Invested",
    "Cur Value",
    "R-PnL",
    "U-PnL",
    "Updated",
    "Action",
  ];

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="trading-card overflow-hidden">
      <h3 className="mb-3 text-[11px] font-medium uppercase tracking-widest text-muted-foreground">Open Positions</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              {cols.map((h) => (
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
                  {Array.from({ length: cols.length }).map((__, j) => (
                    <td key={j} className="py-2 pr-3">
                      <div className="h-4 w-16 animate-pulse rounded bg-muted" />
                    </td>
                  ))}
                </tr>
              ))
              : null}

            {!isLoading && !positions?.length ? (
              <tr>
                <td colSpan={cols.length} className="py-6 text-center text-sm text-muted-foreground">
                  No open positions
                </td>
              </tr>
            ) : null}

            {!isLoading
              ? positions?.map((p, i) => {
                const uPnl = p.unrealized_pnl_usd || 0;
                const rPnl = p.realized_pnl_usd || 0;
                const qty = p.quantity || 0;
                const curPrice = p.current_price_cents || 0;
                const curValue = Math.max((p.invested_usd || 0) + uPnl, 0);
                return (
                  <tr key={`${p.market_id}-${p.outcome}-${i}`} className="border-b border-border/30 transition-colors hover:bg-secondary/30">
                    <td className="max-w-[220px] py-2 pr-3 text-xs" title={p.market_title || p.market_id || ""}>
                      <span className="block truncate">
                        {p.market_title || shortId(p.market_id || "")}
                      </span>
                      {categoryBadge(p.market_category)}
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{p.outcome || "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs font-semibold uppercase">{p.side || "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{qty.toFixed(1)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{cents(p.avg_price_cents || 0)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{cents(curPrice)}</td>
                    <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(p.invested_usd || 0)}</td>
                    <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(curValue)}</td>
                    <td className={`py-2 pr-3 font-mono text-xs font-bold ${pnlClass(rPnl)}`}>{money(rPnl)}</td>
                    <td className={`py-2 pr-3 font-mono text-xs font-bold ${pnlClass(uPnl)}`}>{money(uPnl)}</td>
                    <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">
                      {(p.updated_at || "-").replace("T", " ").slice(0, 19)}
                    </td>
                    <td className="py-2">
                      <button
                        onClick={() => handleClose(p.id)}
                        disabled={isClosing && closingId === p.id}
                        className="flex h-6 w-6 items-center justify-center rounded bg-destructive/10 text-destructive transition-colors hover:bg-destructive hover:text-destructive-foreground disabled:opacity-50"
                        title="Force Sell Market Price"
                      >
                        {isClosing && closingId === p.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <X className="h-3 w-3" />
                        )}
                      </button>
                    </td>
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
