import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return;
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const [rawKey, ...rawValue] = trimmed.split("=");
    const key = rawKey.trim();
    const value = rawValue.join("=").trim().replace(/^['"]|['"]$/g, "");
    if (!process.env[key]) process.env[key] = value;
  }
}

loadDotEnv(path.join(rootDir, ".env"));

function boolEnv(name, fallback = false) {
  const value = process.env[name];
  if (value == null || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

export const config = {
  rootDir,
  port: Number(process.env.PORT || 8787),
  baseUrl: (process.env.BASE_URL || `http://localhost:${process.env.PORT || 8787}`).replace(/\/$/, ""),
  dataPath: path.resolve(rootDir, process.env.DATA_PATH || "./data/db.json"),
  managerBotToken: process.env.MANAGER_BOT_TOKEN || "",
  managerBotUsername: (process.env.MANAGER_BOT_USERNAME || "YourManagerBot").replace(/^@/, ""),
  telegramWebhookSecret: process.env.TELEGRAM_WEBHOOK_SECRET || "local-dev-secret",
  telegramPolling: boolEnv("TELEGRAM_POLLING", false),
  telegramPollTimeout: Number(process.env.TELEGRAM_POLL_TIMEOUT || 25),
  openaiApiKey: process.env.OPENAI_API_KEY || "",
  openaiModel: process.env.OPENAI_MODEL || "gpt-4.1-mini",
  openaiBaseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1/chat/completions",
  openrouterApiKey: process.env.OPENROUTER_API_KEY || "",
  openrouterModel: process.env.OPENROUTER_MODEL || "openai/gpt-4.1-mini",
  openrouterBaseUrl:
    process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1/chat/completions",
  openrouterReferer: process.env.OPENROUTER_REFERER || process.env.BASE_URL || `http://localhost:${process.env.PORT || 8787}`,
  openrouterTitle: process.env.OPENROUTER_TITLE || "TG Sales Agent",
  encryptionKey: process.env.ENCRYPTION_KEY || "",
  defaultSheetWebhookUrl: process.env.DEFAULT_SHEET_WEBHOOK_URL || "",
  defaultCrmWebhookUrl: process.env.DEFAULT_CRM_WEBHOOK_URL || "",
  trustLocalTokenStorage: boolEnv("TRUST_LOCAL_TOKEN_STORAGE", true)
};
