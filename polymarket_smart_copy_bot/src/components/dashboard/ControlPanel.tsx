import { useState } from "react";
import type { ElementType } from "react";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  FileText,
  Filter,
  Pause,
  Play,
  Rocket,
  Settings2,
  Shield,
  Trash2,
  UserPlus,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import type { BotStatus } from "@/lib/api";
import { postControl } from "@/lib/api";

interface Props {
  status: BotStatus | undefined;
}

function ControlButton({
  onClick,
  disabled,
  icon: Icon,
  label,
  variant,
}: {
  onClick: () => void;
  disabled?: boolean;
  icon: ElementType;
  label: string;
  variant: "profit" | "loss" | "warn" | "accent";
}) {
  const base =
    "flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-bold uppercase tracking-wider transition-all duration-200 disabled:cursor-not-allowed disabled:opacity-40";
  const variants = {
    profit: "border border-profit/30 bg-profit/15 text-profit hover:border-profit/50 hover:bg-profit/25",
    loss: "border border-loss/30 bg-loss/15 text-loss hover:border-loss/50 hover:bg-loss/25",
    warn: "border border-warning/30 bg-warning/15 text-warning hover:border-warning/50 hover:bg-warning/25",
    accent: "border border-primary/30 bg-primary/15 text-primary hover:border-primary/50 hover:bg-primary/25",
  };

  return (
    <button onClick={onClick} disabled={disabled} className={`${base} ${variants[variant]}`}>
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}

export function ControlPanel({ status }: Props) {
  const qc = useQueryClient();
  const [showSettings, setShowSettings] = useState(false);
  const [apiUrl, setApiUrl] = useState(localStorage.getItem("bot_api_url") || "");
  const [token, setToken] = useState(localStorage.getItem("dashboard_write_token") || "");

  const mutate = useMutation({
    mutationFn: ({ path, payload }: { path: string; payload: Record<string, unknown> }) => postControl(path, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["bot-status"] });
      qc.invalidateQueries({ queryKey: ["bot-trades"] });
      qc.invalidateQueries({ queryKey: ["bot-positions"] });
    },
    onError: (err: Error) => {
      toast.error(`Ошибка: ${err.message}`);
    },
  });

  const act = (path: string, payload: Record<string, unknown>, msg: string, confirmMessage?: string) => {
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    mutate.mutate(
      { path, payload },
      {
        onSuccess: () => toast.success(msg),
      },
    );
  };

  const saveSettings = () => {
    localStorage.setItem("bot_api_url", apiUrl);
    localStorage.setItem("dashboard_write_token", token);
    toast.success("Настройки сохранены");
    setShowSettings(false);
    qc.invalidateQueries();
  };

  const trading = status?.trading_enabled;
  const dry = status?.dry_run;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <ControlButton
          icon={Play}
          label="Start"
          variant="profit"
          disabled={trading || mutate.isPending}
          onClick={() => act("/control/trading", { enabled: true, run_now: true }, "Торговля включена")}
        />
        <ControlButton
          icon={Pause}
          label="Stop"
          variant="loss"
          disabled={!trading || mutate.isPending}
          onClick={() => act("/control/trading", { enabled: false }, "Торговля на паузе")}
        />
        <div className="mx-1 h-6 w-px bg-border" />
        <ControlButton
          icon={FileText}
          label="Paper"
          variant="warn"
          disabled={dry || mutate.isPending}
          onClick={() => act("/control/engine", { dry_run: true }, "Paper mode")}
        />
        <ControlButton
          icon={Zap}
          label="Live"
          variant="loss"
          disabled={!dry || mutate.isPending}
          onClick={() =>
            act(
              "/control/engine",
              { dry_run: false },
              "LIVE mode",
              "Переключить в LIVE MODE? Это реальная торговля.",
            )
          }
        />
        <div className="mx-1 h-6 w-px bg-border" />
        <ControlButton
          icon={Shield}
          label="Conservative"
          variant="accent"
          disabled={mutate.isPending}
          onClick={() => act("/control/mode", { mode: "conservative" }, "Conservative mode")}
        />
        <ControlButton
          icon={Rocket}
          label="Aggressive"
          variant="warn"
          disabled={mutate.isPending}
          onClick={() => act("/control/mode", { mode: "aggressive" }, "Aggressive mode")}
        />
        <div className="mx-1 h-6 w-px bg-border" />
        <ControlButton
          icon={Zap}
          label={`Boost ${status?.high_conviction_boost_enabled ? "OFF" : "ON"}`}
          variant="accent"
          disabled={mutate.isPending}
          onClick={() =>
            act(
              "/control/boost",
              { enabled: !status?.high_conviction_boost_enabled },
              `Boost ${!status?.high_conviction_boost_enabled ? "ON" : "OFF"}`,
            )
          }
        />
        <ControlButton
          icon={Filter}
          label={`Filter ${status?.price_filter_enabled ? "OFF" : "ON"}`}
          variant="accent"
          disabled={mutate.isPending}
          onClick={() =>
            act(
              "/control/price-filter",
              { enabled: !status?.price_filter_enabled },
              `Price filter ${!status?.price_filter_enabled ? "ON" : "OFF"}`,
            )
          }
        />
        <ControlButton
          icon={UserPlus}
          label={`AutoAdd ${status?.discovery_autoadd ? "OFF" : "ON"}`}
          variant="accent"
          disabled={mutate.isPending}
          onClick={() =>
            act(
              "/control/autoadd",
              { enabled: !status?.discovery_autoadd },
              `AutoAdd ${!status?.discovery_autoadd ? "ON" : "OFF"}`,
            )
          }
        />
        <ControlButton
          icon={Trash2}
          label="Cleanup"
          variant="warn"
          disabled={mutate.isPending}
          onClick={() => act("/control/orders/cleanup", {}, "Очистка завершена")}
        />

        <div className="ml-auto">
          <button
            onClick={() => setShowSettings(!showSettings)}
            className="flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <Settings2 className="h-3.5 w-3.5" />
            Настройки
          </button>
        </div>
      </div>

      {showSettings && (
        <div className="trading-card space-y-3">
          <h3 className="text-sm font-semibold text-foreground">Подключение к боту</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">API URL</label>
              <input
                type="text"
                value={apiUrl}
                onChange={(e) => setApiUrl(e.target.value)}
                placeholder="https://your-bot.railway.app"
                className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm font-mono text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
            <div>
              <label className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">Write Token</label>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Dashboard write token"
                className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm font-mono text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
          </div>
          <button
            onClick={saveSettings}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-bold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Сохранить
          </button>
        </div>
      )}
    </div>
  );
}
