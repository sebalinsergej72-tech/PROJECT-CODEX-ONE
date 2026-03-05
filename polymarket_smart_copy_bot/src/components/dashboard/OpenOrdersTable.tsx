import { motion } from "framer-motion";

import type { OpenOrder } from "@/lib/api";

interface Props {
  orders: OpenOrder[] | undefined;
  isLoading: boolean;
}

function money(v: number) {
  return `$${v.toFixed(2)}`;
}

function shortAddr(v: string | null | undefined) {
  if (!v) return "—";
  return v.length < 12 ? v : `${v.slice(0, 6)}…${v.slice(-4)}`;
}

export function OpenOrdersTable({ orders, isLoading }: Props) {
  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="trading-card overflow-hidden">
      <h3 className="mb-3 text-[11px] font-medium uppercase tracking-widest text-muted-foreground">Open Orders</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              {["Market", "Outcome", "Side", "Price", "Shares", "Notional", "Status", "Wallet", "Created"].map((h) => (
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
                    {Array.from({ length: 9 }).map((__, j) => (
                      <td key={j} className="py-2 pr-3">
                        <div className="h-4 w-16 animate-pulse rounded bg-muted" />
                      </td>
                    ))}
                  </tr>
                ))
              : null}

            {!isLoading && !orders?.length ? (
              <tr>
                <td colSpan={9} className="py-6 text-center text-sm text-muted-foreground">
                  No open orders
                </td>
              </tr>
            ) : null}

            {!isLoading
              ? orders?.map((order) => (
                  <tr key={order.order_id} className="border-b border-border/30 transition-colors hover:bg-secondary/30">
                    <td className="max-w-[140px] truncate py-2 pr-3 font-mono text-xs">{order.market_id || "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{order.outcome || "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs uppercase">{order.side || "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{(order.price_cents || 0).toFixed(1)}¢</td>
                    <td className="py-2 pr-3 font-mono text-xs">{(order.size_shares || 0).toFixed(2)}</td>
                    <td className="py-2 pr-3 font-mono text-xs font-semibold">{money(order.notional_usd_estimate || 0)}</td>
                    <td className="py-2 pr-3 font-mono text-xs uppercase text-warning">{order.trade_status || "open"}</td>
                    <td className="py-2 pr-3 font-mono text-xs text-muted-foreground">{shortAddr(order.wallet_address)}</td>
                    <td className="py-2 font-mono text-xs text-muted-foreground">
                      {(order.created_at || "-").replace("T", " ").slice(0, 19)}
                    </td>
                  </tr>
                ))
              : null}
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}
