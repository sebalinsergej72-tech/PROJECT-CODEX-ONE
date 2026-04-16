import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { config } from "./config.js";
import { store } from "./storage.js";
import { generateSalesReply } from "./agent.js";
import { newManagedBotLink } from "./telegram.js";
import { handleManagedUpdate, handleManagerUpdate, suggestedUsername } from "./bot-handlers.js";
import { getManagerStatus } from "./bot-runner.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(__dirname, "../public");

function json(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(data)
  });
  res.end(data);
}

function text(res, status, body, contentType = "text/plain; charset=utf-8") {
  res.writeHead(status, { "Content-Type": contentType });
  res.end(body);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw) return {};
  return JSON.parse(raw);
}

function route(req) {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  return { method: req.method || "GET", pathname: url.pathname, query: url.searchParams };
}

function safeBusinessPatch(input) {
  const allowed = [
    "name",
    "niche",
    "tone",
    "timezone",
    "managerChatId",
    "ownerTelegramId",
    "sheetWebhookUrl",
    "crmWebhookUrl",
    "address",
    "workingHours",
    "handoffPolicy",
    "catalog",
    "faq",
    "leadQuestions",
    "followUp"
  ];
  return Object.fromEntries(Object.entries(input).filter(([key]) => allowed.includes(key)));
}

async function api(req, res, method, pathname, query) {
  if (method === "GET" && pathname === "/api/health") {
    const llmProvider = config.openrouterApiKey ? "openrouter" : config.openaiApiKey ? "openai" : "local";
    return json(res, 200, {
      ok: true,
      service: "tg-sales-agent",
      baseUrl: config.baseUrl,
      managerConfigured: Boolean(config.managerBotToken && config.managerBotUsername),
      pollingEnabled: config.telegramPolling,
      llmProvider,
      openaiConfigured: Boolean(config.openaiApiKey),
      openrouterConfigured: Boolean(config.openrouterApiKey)
    });
  }

  if (method === "GET" && pathname === "/api/businesses") {
    return json(res, 200, { businesses: store.listBusinesses() });
  }

  if (method === "GET" && pathname === "/api/telegram/manager-status") {
    return json(res, 200, { manager: await getManagerStatus() });
  }

  if (method === "POST" && pathname === "/api/businesses") {
    const body = await readJson(req);
    return json(res, 201, { business: store.createBusiness(safeBusinessPatch(body)) });
  }

  const businessMatch = pathname.match(/^\/api\/businesses\/([^/]+)$/);
  if (businessMatch && method === "GET") {
    const business = store.getBusiness(businessMatch[1]);
    return business ? json(res, 200, { business }) : json(res, 404, { error: "business not found" });
  }

  if (businessMatch && method === "PUT") {
    const body = await readJson(req);
    const business = store.updateBusiness(businessMatch[1], safeBusinessPatch(body));
    return business ? json(res, 200, { business }) : json(res, 404, { error: "business not found" });
  }

  if (method === "GET" && pathname === "/api/dashboard") {
    return json(res, 200, { dashboard: store.dashboard(query.get("businessId")) });
  }

  if (method === "GET" && pathname === "/api/bots") {
    return json(res, 200, { bots: store.listBots(query.get("businessId")) });
  }

  if (method === "GET" && pathname === "/api/leads") {
    return json(res, 200, { leads: store.listLeads(query.get("businessId")) });
  }

  const leadMatch = pathname.match(/^\/api\/leads\/([^/]+)$/);
  if (leadMatch && method === "PATCH") {
    const lead = store.updateLead(leadMatch[1], await readJson(req));
    return lead ? json(res, 200, { lead }) : json(res, 404, { error: "lead not found" });
  }

  if (method === "GET" && pathname === "/api/conversations") {
    return json(res, 200, { conversations: store.listConversations(query.get("businessId")) });
  }

  if (method === "GET" && pathname === "/api/newbot-link") {
    const business = store.getBusiness(query.get("businessId")) || store.listBusinesses()[0];
    const link = newManagedBotLink({
      suggestedName: `${business?.name || "Sales"} Assistant`,
      suggestedUsername: suggestedUsername(business?.name || "Sales")
    });
    return json(res, 200, { link });
  }

  if (method === "POST" && pathname === "/api/simulate") {
    const body = await readJson(req);
    const business = store.getBusiness(body.businessId);
    if (!business) return json(res, 404, { error: "business not found" });
    const bot = store.listBots(business.id)[0] || { id: "simulated_bot" };
    let conversation = store.upsertConversation({
      businessId: business.id,
      botId: bot.id,
      chatId: body.chatId || "simulator",
      user: { id: "simulator", first_name: "Demo" },
      message: body.text,
      role: "user"
    });
    const result = await generateSalesReply({
      business,
      conversation,
      text: body.text,
      user: { id: "simulator", first_name: "Demo" },
      openai: config.openrouterApiKey
        ? {
            provider: "openrouter",
            apiKey: config.openrouterApiKey,
            model: config.openrouterModel,
            baseUrl: config.openrouterBaseUrl,
            headers: {
              "HTTP-Referer": config.openrouterReferer,
              "X-Title": config.openrouterTitle
            }
          }
        : {
            provider: "openai",
            apiKey: config.openaiApiKey,
            model: config.openaiModel,
            baseUrl: config.openaiBaseUrl,
            headers: {}
          }
    });
    conversation = store.upsertConversation({
      businessId: business.id,
      botId: bot.id,
      chatId: body.chatId || "simulator",
      message: result.reply,
      role: "assistant",
      leadDraft: result.leadDraft
    });
    let lead = null;
    if (result.shouldCreateLead) {
      lead = store.upsertLead({
        businessId: business.id,
        botId: bot.id,
        conversationId: conversation.id,
        user: { id: "simulator", first_name: "Demo" },
        patch: result.leadPatch,
        source: "simulator"
      });
    }
    return json(res, 200, { reply: result.reply, result, lead });
  }

  return false;
}

async function telegram(req, res, method, pathname) {
  if (method !== "POST") return false;

  const managerMatch = pathname.match(/^\/telegram\/manager\/([^/]+)$/);
  if (managerMatch) {
    if (managerMatch[1] !== config.telegramWebhookSecret) return json(res, 403, { error: "bad secret" });
    const result = await handleManagerUpdate(await readJson(req));
    return json(res, 200, result);
  }

  const managedMatch = pathname.match(/^\/telegram\/managed\/([^/]+)\/([^/]+)$/);
  if (managedMatch) {
    if (managedMatch[2] !== config.telegramWebhookSecret) return json(res, 403, { error: "bad secret" });
    const result = await handleManagedUpdate(managedMatch[1], await readJson(req));
    return json(res, 200, result);
  }

  return false;
}

function serveStatic(res, pathname) {
  const safePath = pathname === "/" ? "/index.html" : pathname;
  const filePath = path.resolve(publicDir, `.${safePath}`);
  if (!filePath.startsWith(publicDir) || !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    return false;
  }
  const ext = path.extname(filePath);
  const types = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml"
  };
  text(res, 200, fs.readFileSync(filePath), types[ext] || "application/octet-stream");
  return true;
}

export async function handleRequest(req, res) {
  const { method, pathname, query } = route(req);
  try {
    if (pathname.startsWith("/api/")) {
      const handled = await api(req, res, method, pathname, query);
      if (handled !== false) return;
    }
    if (pathname.startsWith("/telegram/")) {
      const handled = await telegram(req, res, method, pathname);
      if (handled !== false) return;
    }
    if (serveStatic(res, pathname)) return;
    json(res, 404, { error: "not found" });
  } catch (error) {
    store.appendEvent("http.error", { pathname, message: error.message, stack: error.stack });
    json(res, 500, { error: error.message });
  }
}
