import { config } from "./config.js";
import { store } from "./storage.js";
import { generateSalesReply } from "./agent.js";
import { syncLead } from "./integrations.js";
import {
  getManagedBotToken,
  newManagedBotLink,
  requestManagedBotKeyboard,
  sendMessage,
  setWebhook,
  userLabel
} from "./telegram.js";

function llmConfig() {
  if (config.openrouterApiKey) {
    return {
      provider: "openrouter",
      apiKey: config.openrouterApiKey,
      model: config.openrouterModel,
      baseUrl: config.openrouterBaseUrl,
      headers: {
        "HTTP-Referer": config.openrouterReferer,
        "X-Title": config.openrouterTitle
      }
    };
  }

  if (config.openaiApiKey) {
    return {
      provider: "openai",
      apiKey: config.openaiApiKey,
      model: config.openaiModel,
      baseUrl: config.openaiBaseUrl,
      headers: {}
    };
  }

  return { provider: "local", apiKey: "", model: "", baseUrl: "", headers: {} };
}

export function suggestedUsername(name) {
  const ascii = String(name || "SalesAgent")
    .normalize("NFKD")
    .replace(/[^\w\s]/g, "")
    .replace(/\s+/g, "")
    .slice(0, 24);
  return `${ascii || "Sales"}AgentBot`;
}

async function notifyManager({ business, lead }) {
  if (!business.managerChatId || !config.managerBotToken) return;
  const fields = lead.fields || {};
  const lines = [
    "Новая заявка",
    "",
    `Бизнес: ${business.name}`,
    `Статус: ${lead.status}`,
    `Скоринг: ${lead.score}/100`,
    fields.name ? `Имя: ${fields.name}` : "",
    fields.telegram ? `Telegram: ${fields.telegram}` : "",
    fields.contact ? `Контакт: ${fields.contact}` : "",
    fields.interest ? `Интерес: ${fields.interest}` : "",
    fields.desiredTime ? `Когда: ${fields.desiredTime}` : "",
    "",
    lead.summary
  ].filter(Boolean);
  await sendMessage(config.managerBotToken, business.managerChatId, lines.join("\n"));
}

export async function handleManagerUpdate(update) {
  store.appendEvent("telegram.manager.update", update);

  const message = update.message || update.edited_message;
  const managed = update.managed_bot || message?.managed_bot_created;

  if (managed?.bot) {
    const owner = managed.user || message?.from || {};
    const ownerChatId = message?.chat?.id || owner.id;
    const business = store.getOrCreateBusinessForOwner(owner.id, {
      managerChatId: ownerChatId,
      name: `${userLabel(owner)} business`
    });
    const botToken = await getManagedBotToken(config.managerBotToken, managed.bot.id);
    const webhookUrl = `${config.baseUrl}/telegram/managed/${managed.bot.id}/${config.telegramWebhookSecret}`;
    const bot = store.upsertManagedBot({
      businessId: business.id,
      telegramBotId: managed.bot.id,
      username: managed.bot.username,
      firstName: managed.bot.first_name,
      token: botToken,
      webhookUrl
    });
    if (!config.telegramPolling) {
      await setWebhook(botToken, webhookUrl, config.telegramWebhookSecret);
    }
    if (ownerChatId && config.managerBotToken) {
      await sendMessage(
        config.managerBotToken,
        ownerChatId,
        `Готово: @${managed.bot.username} подключен как AI-продавец.\n\nАдминка: ${config.baseUrl}/?business=${business.id}`
      );
    }
    return { ok: true, business, bot };
  }

  if (message?.text?.startsWith("/start")) {
    const owner = message.from || {};
    const business = store.getOrCreateBusinessForOwner(owner.id, {
      managerChatId: message.chat.id,
      name: `${userLabel(owner)} business`
    });
    const link = newManagedBotLink({
      suggestedName: `${business.name} Assistant`,
      suggestedUsername: suggestedUsername(business.name)
    });
    await sendMessage(
      config.managerBotToken,
      message.chat.id,
      `Я помогу создать брендированного AI-продавца для Telegram.\n\nСсылка создания: ${link}\n\nПосле создания я подключу бота, базу знаний и лиды.`
    );
    await requestManagedBotKeyboard(
      config.managerBotToken,
      message.chat.id,
      `${business.name} Assistant`,
      suggestedUsername(business.name)
    );
    return { ok: true, business };
  }

  if (message?.text?.startsWith("/settings")) {
    const business = store.getBusiness(message.from?.id);
    await sendMessage(
      config.managerBotToken,
      message.chat.id,
      business ? `Настройки: ${config.baseUrl}/?business=${business.id}` : `Сначала отправьте /start`
    );
  }

  return { ok: true, ignored: true };
}

export async function handleManagedUpdate(botId, update) {
  store.appendEvent("telegram.managed.update", { botId, update });
  const bot = store.getBotByTelegramId(botId, true);
  if (!bot) return { ok: false, error: "unknown managed bot" };
  const business = store.getBusiness(bot.businessId);
  if (!business) return { ok: false, error: "unknown business" };

  const message = update.message || update.edited_message;
  if (!message?.chat?.id) return { ok: true, ignored: true };

  const text = message.text || message.caption || "";
  if (!text) {
    await sendMessage(bot.token, message.chat.id, "Пока я лучше всего понимаю текстовые вопросы. Напишите, что вас интересует, и я помогу.");
    return { ok: true };
  }

  const user = message.from || {};
  let conversation = store.upsertConversation({
    businessId: business.id,
    botId: bot.id,
    chatId: message.chat.id,
    user,
    message: text,
    role: "user"
  });

  if (text.startsWith("/start")) {
    const reply = `Здравствуйте! Я AI-продавец ${business.name}. Подскажу по услугам, ценам и записи. Что вас интересует?`;
    await sendMessage(bot.token, message.chat.id, reply);
    store.upsertConversation({
      businessId: business.id,
      botId: bot.id,
      chatId: message.chat.id,
      message: reply,
      role: "assistant"
    });
    return { ok: true };
  }

  const result = await generateSalesReply({
    business,
    conversation,
    text,
    user,
    openai: llmConfig()
  });

  conversation = store.upsertConversation({
    businessId: business.id,
    botId: bot.id,
    chatId: message.chat.id,
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
      user,
      patch: result.leadPatch
    });
  }

  await sendMessage(bot.token, message.chat.id, result.reply);

  if (lead) {
    await syncLead({ business, lead }).catch((error) => {
      store.appendEvent("integration.sync.error", { leadId: lead.id, message: error.message });
    });
    if (result.shouldHandoff || lead.score >= 70) {
      await notifyManager({ business, lead }).catch((error) => {
        store.appendEvent("telegram.manager.notify.error", { leadId: lead.id, message: error.message });
      });
    }
  }

  return { ok: true, lead, result };
}
