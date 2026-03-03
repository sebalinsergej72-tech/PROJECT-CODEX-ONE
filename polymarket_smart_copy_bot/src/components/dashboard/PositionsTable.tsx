import { motion } from "framer-motion";

import type { Position } from "@/lib/api";

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

export function PositionsTable({ positions, isLoading }: Props) {
  const cols = ["Market", "Outcome", "Side", "Qty", "Avg Price", "Cur Price", "Invested", "Cur Value", "U-PnL", "Updated"];

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
                  const qty = p.quantity || 0;
                  const curPrice = p.current_price_cents || 0;
                  const curValue = (qty * curPrice) / 100;
                  return (
                    <tr key={`${p.market_id}-${p.outcome}-${i}`} className="border-b border-border/30 transition-colors hover:bg-secondary/30">
                      <td className="max-w-[140px] truncate py-2 pr-3 font-mono text-xs">{p.market_id || "-"}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{p.outcome || "-"}</td>
                      <td className="py-2 pr-3 font-mono text-xs font-semibold uppercase">{p.side || "-"}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{qty.toFixed(1)}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{cents(p.avg_price_cents || 0)}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{cents(curPrice)}</td>
                      <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(p.invested_usd || 0)}</td>
                      <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(curValue)}</td>
                      <td className={`py-2 pr-3 font-mono text-xs font-bold ${pnlClass(uPnl)}`}>{money(uPnl)}</td>
                      <td className="py-2 font-mono text-xs text-muted-foreground">
                        {(p.updated_at || "-").replace("T", " ").slice(0, 19)}
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
