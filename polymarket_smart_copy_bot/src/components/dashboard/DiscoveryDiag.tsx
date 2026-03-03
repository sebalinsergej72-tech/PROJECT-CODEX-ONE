import { Search } from "lucide-react";

import type { BotStatus } from "@/lib/api";

interface Props {
  status: BotStatus | undefined;
}

export function DiscoveryDiag({ status }: Props) {
  if (!status) return null;

  const scanned = status.discovery_scanned_candidates || status.last_discovery_stats?.total_candidates || 0;
  const passed = status.discovery_passed_filters || status.last_discovery_stats?.passed_all_filters || 0;
  const stats = status.discovery_filter_stats || {};
  const topReasons = Object.entries(stats)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 5);

  return (
    <div className="trading-card">
      <div className="mb-2 flex items-center gap-2">
        <Search className="h-3.5 w-3.5 text-muted-foreground" />
        <h3 className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
          Discovery Diagnostics
        </h3>
      </div>
      <div className="flex flex-wrap items-center gap-3 font-mono text-sm">
        <span className="text-muted-foreground">
          scanned=<span className="font-bold text-foreground">{scanned}</span>
        </span>
        <span className="text-muted-foreground">
          passed=<span className="font-bold text-profit">{passed}</span>
        </span>
        {topReasons.length > 0 ? (
          <>
            <span className="text-border">|</span>
            <span className="text-muted-foreground">rejected:</span>
            {topReasons.map(([k, v]) => (
              <span key={k} className="rounded bg-loss/10 px-1.5 py-0.5 text-xs text-loss">
                {k}={v}
              </span>
            ))}
          </>
        ) : null}
      </div>
    </div>
  );
}
