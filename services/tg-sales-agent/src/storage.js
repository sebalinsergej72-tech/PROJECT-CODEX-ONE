import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { config } from "./config.js";
import { open, seal } from "./crypto-box.js";

const now = () => new Date().toISOString();
const id = (prefix) => `${prefix}_${crypto.randomBytes(8).toString("hex")}`;

const starterFaq = [
  {
    q: "Сколько стоит услуга?",
    a: "Стоимость зависит от услуги и объема. Я уточню, что именно вам интересно, и передам администратору заявку с деталями."
  },
  {
    q: "Как записаться?",
    a: "Напишите, какая услуга нужна и когда вам удобно. Я проверю детали и передам заявку администратору."
  },
  {
    q: "Где вы находитесь?",
    a: "Адрес можно указать в настройках бизнеса. Если адрес еще не заполнен, я передам вопрос администратору."
  }
];

const starterCatalog = [
  {
    name: "Консультация",
    price: "от 0 до 3000 ₽",
    description: "Первичный разбор запроса, рекомендации и подбор следующего шага."
  }
];

function emptyDb() {
  return {
    meta: { createdAt: now(), version: 1 },
    businesses: [],
    bots: [],
    conversations: [],
    leads: [],
    events: []
  };
}

export class JsonStore {
  constructor(filePath = config.dataPath) {
    this.filePath = filePath;
    this.ensure();
  }

  ensure() {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    if (!fs.existsSync(this.filePath)) {
      fs.writeFileSync(this.filePath, `${JSON.stringify(emptyDb(), null, 2)}\n`);
    }
  }

  read() {
    this.ensure();
    return JSON.parse(fs.readFileSync(this.filePath, "utf8"));
  }

  write(db) {
    const tmp = `${this.filePath}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(db, null, 2)}\n`);
    fs.renameSync(tmp, this.filePath);
  }

  mutate(fn) {
    const db = this.read();
    const result = fn(db);
    this.write(db);
    return result;
  }

  createBusiness(input = {}) {
    return this.mutate((db) => {
      const business = {
        id: input.id || id("biz"),
        name: input.name || "Новый бизнес",
        niche: input.niche || "service_business",
        tone: input.tone || "friendly_expert",
        timezone: input.timezone || "Europe/Moscow",
        managerChatId: input.managerChatId || null,
        ownerTelegramId: input.ownerTelegramId || null,
        sheetWebhookUrl: input.sheetWebhookUrl ?? config.defaultSheetWebhookUrl,
        crmWebhookUrl: input.crmWebhookUrl ?? config.defaultCrmWebhookUrl,
        address: input.address || "",
        workingHours: input.workingHours || "Ежедневно, 10:00-20:00",
        handoffPolicy: input.handoffPolicy || "Передавать человеку, если клиент готов записаться, просит скидку, задает медицинский/юридический вопрос или недоволен.",
        catalog: input.catalog?.length ? input.catalog : starterCatalog,
        faq: input.faq?.length ? input.faq : starterFaq,
        leadQuestions: input.leadQuestions?.length
          ? input.leadQuestions
          : ["Какая услуга интересует?", "Когда удобно?", "Как с вами связаться?"],
        followUp: input.followUp || { enabled: true, delayMinutes: 180, maxMessages: 1 },
        createdAt: now(),
        updatedAt: now()
      };
      db.businesses.push(business);
      return business;
    });
  }

  listBusinesses() {
    return this.read().businesses;
  }

  getBusiness(idOrOwner) {
    const db = this.read();
    return db.businesses.find((b) => b.id === idOrOwner || String(b.ownerTelegramId) === String(idOrOwner));
  }

  getOrCreateBusinessForOwner(ownerTelegramId, defaults = {}) {
    const existing = this.getBusiness(ownerTelegramId);
    if (existing) return existing;
    return this.createBusiness({
      name: defaults.name || `Telegram бизнес ${ownerTelegramId}`,
      ownerTelegramId,
      managerChatId: defaults.managerChatId || ownerTelegramId,
      ...defaults
    });
  }

  updateBusiness(businessId, patch) {
    return this.mutate((db) => {
      const business = db.businesses.find((b) => b.id === businessId);
      if (!business) return null;
      Object.assign(business, patch, { updatedAt: now() });
      return business;
    });
  }

  upsertManagedBot(input) {
    return this.mutate((db) => {
      let bot = db.bots.find((b) => String(b.telegramBotId) === String(input.telegramBotId));
      const tokenBox = input.token ? seal(input.token, config.encryptionKey) : bot?.tokenBox || null;
      if (!bot) {
        bot = {
          id: id("bot"),
          businessId: input.businessId,
          telegramBotId: input.telegramBotId,
          username: input.username || "",
          firstName: input.firstName || "",
          tokenBox,
          webhookUrl: input.webhookUrl || "",
          status: "active",
          createdAt: now(),
          updatedAt: now()
        };
        db.bots.push(bot);
      } else {
        Object.assign(bot, {
          businessId: input.businessId || bot.businessId,
          username: input.username || bot.username,
          firstName: input.firstName || bot.firstName,
          tokenBox,
          webhookUrl: input.webhookUrl || bot.webhookUrl,
          status: input.status || bot.status,
          updatedAt: now()
        });
      }
      return { ...bot, token: undefined };
    });
  }

  listBots(businessId) {
    const bots = this.read().bots.filter((bot) => !businessId || bot.businessId === businessId);
    return bots.map(({ tokenBox, ...bot }) => bot);
  }

  getBotByTelegramId(telegramBotId, includeToken = false) {
    const bot = this.read().bots.find((b) => String(b.telegramBotId) === String(telegramBotId));
    if (!bot) return null;
    if (!includeToken) {
      const { tokenBox, ...safe } = bot;
      return safe;
    }
    return { ...bot, token: open(bot.tokenBox, config.encryptionKey) };
  }

  appendEvent(type, payload = {}) {
    return this.mutate((db) => {
      const event = { id: id("evt"), type, payload, createdAt: now() };
      db.events.unshift(event);
      db.events = db.events.slice(0, 500);
      return event;
    });
  }

  getConversation({ businessId, botId, chatId }) {
    return this.read().conversations.find(
      (c) => c.businessId === businessId && c.botId === botId && String(c.chatId) === String(chatId)
    );
  }

  upsertConversation({ businessId, botId, chatId, user, message, role = "user", leadDraft }) {
    return this.mutate((db) => {
      let conversation = db.conversations.find(
        (c) => c.businessId === businessId && c.botId === botId && String(c.chatId) === String(chatId)
      );
      if (!conversation) {
        conversation = {
          id: id("conv"),
          businessId,
          botId,
          chatId,
          user: user || {},
          messages: [],
          leadDraft: {},
          leadId: null,
          createdAt: now(),
          updatedAt: now()
        };
        db.conversations.unshift(conversation);
      }
      if (user) conversation.user = { ...conversation.user, ...user };
      if (leadDraft) conversation.leadDraft = { ...conversation.leadDraft, ...leadDraft };
      if (message) {
        conversation.messages.push({
          id: id("msg"),
          role,
          text: message,
          createdAt: now()
        });
        conversation.messages = conversation.messages.slice(-40);
      }
      conversation.updatedAt = now();
      return conversation;
    });
  }

  upsertLead({ businessId, botId, conversationId, user, patch, source = "telegram" }) {
    return this.mutate((db) => {
      const conversation = db.conversations.find((c) => c.id === conversationId);
      let lead = conversation?.leadId ? db.leads.find((l) => l.id === conversation.leadId) : null;
      if (!lead) {
        lead = {
          id: id("lead"),
          businessId,
          botId,
          conversationId,
          source,
          user: user || {},
          status: "new",
          score: 0,
          fields: {},
          summary: "",
          handoffReason: "",
          createdAt: now(),
          updatedAt: now()
        };
        db.leads.unshift(lead);
        if (conversation) conversation.leadId = lead.id;
      }
      lead.user = { ...lead.user, ...(user || {}) };
      lead.fields = { ...lead.fields, ...(patch.fields || {}) };
      lead.status = patch.status || lead.status;
      lead.score = Math.max(lead.score || 0, patch.score || 0);
      lead.summary = patch.summary || lead.summary;
      lead.handoffReason = patch.handoffReason || lead.handoffReason;
      lead.updatedAt = now();
      return lead;
    });
  }

  listLeads(businessId) {
    return this.read().leads.filter((lead) => !businessId || lead.businessId === businessId);
  }

  updateLead(leadId, patch) {
    return this.mutate((db) => {
      const lead = db.leads.find((l) => l.id === leadId);
      if (!lead) return null;
      Object.assign(lead, patch, { updatedAt: now() });
      return lead;
    });
  }

  listConversations(businessId) {
    return this.read().conversations.filter((c) => !businessId || c.businessId === businessId);
  }

  dashboard(businessId) {
    const db = this.read();
    const leads = db.leads.filter((l) => !businessId || l.businessId === businessId);
    const conversations = db.conversations.filter((c) => !businessId || c.businessId === businessId);
    const hotLeads = leads.filter((l) => l.score >= 70 || l.status === "hot").length;
    return {
      leads: leads.length,
      hotLeads,
      conversations: conversations.length,
      handoffs: leads.filter((l) => l.status === "handoff").length,
      booked: leads.filter((l) => l.status === "booked").length
    };
  }
}

export const store = new JsonStore();
