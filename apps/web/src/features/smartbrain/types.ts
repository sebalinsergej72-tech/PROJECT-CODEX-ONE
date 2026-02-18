export type SmartBrainMode = "paper" | "live";

export interface SmartBrainSettings {
  enabled: boolean;
  mode: SmartBrainMode;
  riskLevel: "low" | "medium" | "high";
  maxLeverage: number;
  assetsWhitelist: string[];
  emergencyStop: boolean;
}

export interface SmartBrainMetrics {
  sharpe: number;
  profitFactor: number;
  maxDrawdown: number;
  winRate: number;
  modelVersion: string;
  lastRetrainAt: string;
}

export interface SmartBrainLog {
  id: string;
  level: "info" | "warn" | "error";
  message: string;
  createdAt: string;
}
