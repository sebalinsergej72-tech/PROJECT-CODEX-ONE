import { config } from "./config.js";

export async function telegramApi(token, method, payload = {}) {
  if (!token) throw new Error(`Telegram token is required for ${method}`);
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(`Telegram ${method} failed: ${data.description || response.statusText}`);
  }
  return data.result;
}

export function newManagedBotLink({ managerUsername = config.managerBotUsername, suggestedUsername, suggestedName }) {
  const username = encodeURIComponent(suggestedUsername || "MySalesAgentBot");
  const name = encodeURIComponent(suggestedName || "AI Sales Agent");
  return `https://t.me/newbot/${encodeURIComponent(managerUsername)}/${username}?name=${name}`;
}

export async function sendMessage(token, chatId, text, extra = {}) {
  return telegramApi(token, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...extra
  });
}

export async function requestManagedBotKeyboard(token, chatId, suggestedName, suggestedUsername) {
  return sendMessage(
    token,
    chatId,
    "Создайте брендированного AI-продавца для бизнеса. Telegram предложит имя и username, их можно поправить перед созданием.",
    {
      reply_markup: {
        resize_keyboard: true,
        one_time_keyboard: true,
        keyboard: [
          [
            {
              text: "Создать AI-продавца",
              request_managed_bot: {
                request_id: Date.now() % 2147483647,
                suggested_name: suggestedName,
                suggested_username: suggestedUsername
              }
            }
          ]
        ]
      }
    }
  );
}

export async function getManagedBotToken(managerToken, userId) {
  return telegramApi(managerToken, "getManagedBotToken", { user_id: userId });
}

export async function setWebhook(token, url, secretToken) {
  return telegramApi(token, "setWebhook", {
    url,
    allowed_updates: ["message", "edited_message", "managed_bot"],
    secret_token: secretToken
  });
}

export async function deleteWebhook(token, dropPendingUpdates = false) {
  return telegramApi(token, "deleteWebhook", { drop_pending_updates: dropPendingUpdates });
}

export async function getMe(token) {
  return telegramApi(token, "getMe");
}

export async function getUpdates(token, payload = {}) {
  return telegramApi(token, "getUpdates", payload);
}

export function userLabel(user = {}) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ");
  return name || (user.username ? `@${user.username}` : `id:${user.id || "unknown"}`);
}
