import { motion } from "framer-motion";
import { RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { ControlPanel } from "@/components/dashboard/ControlPanel";
import { DiscoveryDiag } from "@/components/dashboard/DiscoveryDiag";
import { MetricsGrid } from "@/components/dashboard/MetricsGrid";
import { OpenOrdersTable } from "@/components/dashboard/OpenOrdersTable";
import { PositionsTable } from "@/components/dashboard/PositionsTable";
import { StatusBanner } from "@/components/dashboard/StatusBanner";
import { TradesTable } from "@/components/dashboard/TradesTable";
import { YieldChart } from "@/components/dashboard/YieldChart";
import { LeaderboardTable } from "@/components/dashboard/LeaderboardTable";
import { useBotOpenOrders, useBotPositions, useBotStatus, useBotTrades } from "@/hooks/useBotData";

const Index = () => {
  const qc = useQueryClient();
  const refreshDashboard = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["bot-status"] }),
      qc.invalidateQueries({ queryKey: ["bot-trades"] }),
      qc.invalidateQueries({ queryKey: ["bot-positions"] }),
      qc.invalidateQueries({ queryKey: ["bot-open-orders"] }),
      qc.invalidateQueries({ queryKey: ["bot-portfolio-history"] }),
      qc.invalidateQueries({ queryKey: ["leaderboard"] }),
    ]);
  };
  const {
    data: status,
    isLoading: statusLoading,
    isError: statusError,
    dataUpdatedAt,
  } = useBotStatus();
  const { data: trades, isLoading: tradesLoading } = useBotTrades();
  const { data: positions, isLoading: positionsLoading } = useBotPositions();
  const { data: openOrders, isLoading: openOrdersLoading } = useBotOpenOrders();
  const lastUpdate = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleString([], {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";

  return (
    <div className="min-h-screen bg-background">
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(ellipse_at_15%_20%,hsl(222_50%_15%)_0%,transparent_55%)]" />

      <div className="relative mx-auto max-w-[1400px] px-4 py-6 sm:px-6 lg:px-8">
        <motion.header
          initial={{ opacity: 0, y: -20 }}
          animate={{ opacity: 1, y: 0 }}
          className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
        >
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground sm:text-3xl">Polymarket Smart Copy Bot</h1>
            <p className="mt-1 text-sm text-muted-foreground">Dashboard • PROJECT CODEX ONE</p>
          </div>
          <div className="flex items-center gap-3">
            <span className="font-mono text-xs text-muted-foreground">Updated: {lastUpdate}</span>
            <button
              onClick={() => void refreshDashboard()}
              className="flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-3 py-1.5 text-xs font-medium text-muted-foreground transition-all hover:border-primary/30 hover:text-foreground"
            >
              <RefreshCw className="h-3 w-3" />
              Refresh
            </button>
          </div>
        </motion.header>

        <div className="space-y-4">
          <StatusBanner status={status} isLoading={statusLoading} isError={statusError} />
          <ControlPanel status={status} />
          <YieldChart />
          <LeaderboardTable />
          <MetricsGrid status={status} />
          <DiscoveryDiag status={status} />
          <div className="grid gap-4 lg:grid-cols-2">
            <TradesTable trades={trades} status={status} isLoading={tradesLoading} />
            <PositionsTable positions={positions} isLoading={positionsLoading} />
          </div>
          <OpenOrdersTable orders={openOrders} isLoading={openOrdersLoading} />
        </div>
      </div>
    </div>
  );
};

export default Index;
