import type { SmartBrainLog, SmartBrainMetrics, SmartBrainSettings } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

export async function getSmartBrainState(): Promise<{
  settings: SmartBrainSettings;
  metrics: SmartBrainMetrics;
  logs: SmartBrainLog[];
}> {
  const response = await fetch(`${API_BASE}/smartbrain/state`);
  if (!response.ok) throw new Error("Failed to load SmartBrain state");
  return response.json();
}

export async function updateSmartBrainSettings(settings: SmartBrainSettings): Promise<void> {
  const response = await fetch(`${API_BASE}/smartbrain/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });

  if (!response.ok) throw new Error("Failed to update SmartBrain settings");
}

export async function triggerEmergencyStop(): Promise<void> {
  const response = await fetch(`${API_BASE}/smartbrain/emergency-stop`, { method: "POST" });
  if (!response.ok) throw new Error("Failed to trigger emergency stop");
}
