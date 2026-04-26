import { config } from "./config.js";
import { store } from "./storage.js";
import { handleManagedUpdate, handleManagerUpdate } from "./bot-handlers.js";
import { deleteWebhook, getMe, getUpdates } from "./telegram.js";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function getManagerStatus() {
  if (!config.managerBotToken) {
    return { configured: false };
  }
  try {
    const me = await getMe(config.managerBotToken);
    return {
      configured: true,
      id: me.id,
      username: me.username,
      firstName: me.first_name,
      canManageBots: Boolean(me.can_manage_bots),
      canConnectToBusiness: Boolean(me.can_connect_to_business)
    };
  } catch (error) {
    return { configured: true, error: error.message };
  }
}

async function runPollingLoop({ key, token, allowedUpdates, onUpdate, offsets }) {
  while (true) {
    try {
      const updates = await getUpdates(token, {
        offset: offsets[key] ?? 0,
        timeout: config.telegramPollTimeout,
        allowed_updates: allowedUpdates
      });
      for (const update of updates) {
        offsets[key] = update.update_id + 1;
        await onUpdate(update);
      }
    } catch (error) {
      store.appendEvent("telegram.polling.error", { key, message: error.message });
      await sleep(2500);
    }
  }
}

export async function startPolling() {
  if (!config.telegramPolling || !config.managerBotToken) return { started: false };

  const offsets = {};
  const seenManagedBots = new Set();

  await deleteWebhook(config.managerBotToken, false).catch(() => null);

  runPollingLoop({
    key: "manager",
    token: config.managerBotToken,
    allowedUpdates: ["message", "edited_message", "managed_bot"],
    offsets,
    onUpdate: async (update) => {
      await handleManagerUpdate(update);
    }
  });

  const discoverManagedBots = async () => {
    for (const bot of store.listBots()) {
      if (seenManagedBots.has(bot.telegramBotId)) continue;
      const fullBot = store.getBotByTelegramId(bot.telegramBotId, true);
      if (!fullBot?.token) continue;
      seenManagedBots.add(bot.telegramBotId);
      await deleteWebhook(fullBot.token, false).catch(() => null);
      runPollingLoop({
        key: `managed:${bot.telegramBotId}`,
        token: fullBot.token,
        allowedUpdates: ["message", "edited_message"],
        offsets,
        onUpdate: async (update) => {
          await handleManagedUpdate(bot.telegramBotId, update);
        }
      });
      store.appendEvent("telegram.polling.managed.started", {
        telegramBotId: bot.telegramBotId,
        username: bot.username
      });
    }
  };

  await discoverManagedBots();
  setInterval(discoverManagedBots, 5000).unref();

  return { started: true };
}
