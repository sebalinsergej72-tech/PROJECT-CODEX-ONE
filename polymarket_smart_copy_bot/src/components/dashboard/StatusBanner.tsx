import { motion } from "framer-motion";
import { Activity, AlertTriangle, XCircle } from "lucide-react";

import type { BotStatus } from "@/lib/api";

interface Props {
  status: BotStatus | undefined;
  isLoading: boolean;
  isError: boolean;
}

export function StatusBanner({ status, isLoading, isError }: Props) {
  if (isLoading) {
    return (
      <div className="trading-card animate-pulse">
        <div className="h-6 w-48 rounded bg-muted" />
        <div className="mt-2 h-4 w-64 rounded bg-muted" />
      </div>
    );
  }

  if (isError || !status) {
    return (
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="trading-card glow-border-bad"
      >
        <div className="flex items-center gap-3">
          <XCircle className="h-6 w-6 text-loss" />
          <div>
            <h2 className="font-mono text-lg font-bold text-loss">НЕТ СВЯЗИ С БОТОМ</h2>
            <p className="text-sm text-muted-foreground">Проверьте API URL в настройках и статус deployment</p>
          </div>
        </div>
      </motion.div>
    );
  }

  const { scheduler_running, trading_enabled, dry_run, last_trade_scan_at } = status;
  let variant: "good" | "warn" | "bad" = "bad";
  let title = "";
  let subtitle = "";
  let Icon = Activity;

  if (!scheduler_running) {
    variant = "bad";
    title = "БОТ НЕ РАБОТАЕТ";
    subtitle = "Scheduler остановлен. Проверьте deployment/service.";
    Icon = XCircle;
  } else if (!trading_enabled) {
    variant = "warn";
    title = "БОТ ЗАПУЩЕН • ТОРГОВЛЯ НА ПАУЗЕ";
    subtitle = dry_run
      ? "Paper-mode, исполнение сделок выключено."
      : "Live-mode, исполнение сделок выключено.";
    Icon = AlertTriangle;
  } else {
    variant = "good";
    title = dry_run ? "БОТ РАБОТАЕТ • PAPER MODE" : "БОТ РАБОТАЕТ • LIVE MODE";
    subtitle = `Скан кошельков активен. Последний scan: ${last_trade_scan_at || "n/a"}`;
  }

  const glowClass =
    variant === "good" ? "glow-border-good" : variant === "warn" ? "glow-border-warn" : "glow-border-bad";
  const dotClass = variant === "good" ? "pulse-dot-good" : variant === "warn" ? "pulse-dot-warn" : "pulse-dot-bad";
  const textClass = variant === "good" ? "status-good" : variant === "warn" ? "status-warn" : "status-bad";

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`trading-card ${glowClass}`}
    >
      <div className="flex items-center gap-3">
        <Icon className={`h-6 w-6 ${textClass}`} />
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <span className={`pulse-dot ${dotClass}`} />
            <h2 className={`font-mono text-lg font-bold tracking-wide ${textClass}`}>{title}</h2>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
        </div>
      </div>
    </motion.div>
  );
}
