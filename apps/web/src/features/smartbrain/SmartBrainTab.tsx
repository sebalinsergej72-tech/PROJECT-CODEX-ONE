import { useEffect, useState } from "react";
import { getSmartBrainState, triggerEmergencyStop, updateSmartBrainSettings } from "./api";
import type { SmartBrainLog, SmartBrainMetrics, SmartBrainSettings } from "./types";

const DEFAULT_SETTINGS: SmartBrainSettings = {
  enabled: false,
  mode: "paper",
  riskLevel: "medium",
  maxLeverage: 3,
  assetsWhitelist: ["BTC", "ETH"],
  emergencyStop: false,
};

const DEFAULT_METRICS: SmartBrainMetrics = {
  sharpe: 0,
  profitFactor: 0,
  maxDrawdown: 0,
  winRate: 0,
  modelVersion: "n/a",
  lastRetrainAt: "n/a",
};

export function SmartBrainTab() {
  const [settings, setSettings] = useState<SmartBrainSettings>(DEFAULT_SETTINGS);
  const [metrics, setMetrics] = useState<SmartBrainMetrics>(DEFAULT_METRICS);
  const [logs, setLogs] = useState<SmartBrainLog[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void getSmartBrainState().then((state) => {
      setSettings(state.settings);
      setMetrics(state.metrics);
      setLogs(state.logs);
    });
  }, []);

  async function handleSave() {
    setSaving(true);
    try {
      await updateSmartBrainSettings(settings);
    } finally {
      setSaving(false);
    }
  }

  async function handleEmergencyStop() {
    await triggerEmergencyStop();
    setSettings((prev) => ({ ...prev, emergencyStop: true, enabled: false }));
  }

  return (
    <div className="space-y-6 p-6">
      <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
        <h2 className="text-xl font-semibold text-white">SmartBrain</h2>
        <p className="text-sm text-slate-300">AI decision-maker for autonomous mode.</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <label className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
          <div>Enable AI</div>
          <input
            type="checkbox"
            checked={settings.enabled}
            onChange={(e) => setSettings({ ...settings, enabled: e.target.checked })}
          />
        </label>

        <label className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
          <div>Paper mode</div>
          <input
            type="checkbox"
            checked={settings.mode === "paper"}
            onChange={(e) => setSettings({ ...settings, mode: e.target.checked ? "paper" : "live" })}
          />
        </label>

        <label className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
          <div>Risk level</div>
          <select
            className="mt-2 w-full rounded bg-slate-800 p-2"
            value={settings.riskLevel}
            onChange={(e) => setSettings({ ...settings, riskLevel: e.target.value as SmartBrainSettings["riskLevel"] })}
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </label>

        <label className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
          <div>Max leverage cap</div>
          <input
            type="number"
            min={1}
            max={10}
            className="mt-2 w-full rounded bg-slate-800 p-2"
            value={settings.maxLeverage}
            onChange={(e) => setSettings({ ...settings, maxLeverage: Number(e.target.value) })}
          />
        </label>
      </div>

      <div className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
        <div className="mb-2 font-medium">Assets whitelist (comma-separated)</div>
        <input
          type="text"
          className="w-full rounded bg-slate-800 p-2"
          value={settings.assetsWhitelist.join(",")}
          onChange={(e) => setSettings({ ...settings, assetsWhitelist: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
        />
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <MetricCard label="Sharpe" value={metrics.sharpe.toFixed(2)} />
        <MetricCard label="Profit Factor" value={metrics.profitFactor.toFixed(2)} />
        <MetricCard label="Max DD" value={`${(metrics.maxDrawdown * 100).toFixed(1)}%`} />
        <MetricCard label="Win Rate" value={`${(metrics.winRate * 100).toFixed(1)}%`} />
      </div>

      <div className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
        <div className="mb-2 font-medium">Model status</div>
        <div>Version: {metrics.modelVersion}</div>
        <div>Last retrain: {metrics.lastRetrainAt}</div>
      </div>

      <div className="flex gap-3">
        <button className="rounded bg-cyan-600 px-4 py-2 text-white" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save settings"}
        </button>
        <button className="rounded bg-red-600 px-4 py-2 text-white" onClick={handleEmergencyStop}>
          Emergency stop
        </button>
      </div>

      <div className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
        <div className="mb-2 font-medium">Recent SmartBrain logs</div>
        <div className="space-y-2 text-sm">
          {logs.map((log) => (
            <div key={log.id} className="rounded bg-slate-800 p-2">
              [{log.level}] {log.message}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-slate-700 bg-slate-900 p-4 text-slate-200">
      <div className="text-sm text-slate-400">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  );
}
