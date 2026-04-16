const intentWords = {
  booking: ["запис", "запиш", "бронь", "свобод", "окно", "когда можно", "appointment"],
  price: ["цена", "стоим", "прайс", "сколько", "тариф", "рассроч"],
  location: ["адрес", "где", "локац", "как добраться", "метро"],
  complaint: ["жалоб", "плохо", "недоволен", "верните", "обман", "претенз"],
  risky: ["диагноз", "лечить", "лекарств", "противопоказ", "суд", "иск", "гарантируете"]
};

const serviceSynonyms = {
  консультация: ["консультац", "созвон", "разбор"],
  запись: ["запис", "бронь", "окно"]
};

const stopWords = new Set([
  "и",
  "или",
  "в",
  "во",
  "на",
  "по",
  "не",
  "но",
  "а",
  "ли",
  "есть",
  "нужен",
  "нужна",
  "нужно",
  "сколько",
  "стоит",
  "цена",
  "цены",
  "какая",
  "какой",
  "какие",
  "можно",
  "у",
  "для",
  "от",
  "до",
  "это",
  "как",
  "что",
  "где",
  "когда",
  "мне",
  "меня",
  "вас",
  "ваша",
  "ваш"
]);

function normalize(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s+@.-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function includesAny(text, words) {
  return words.some((word) => text.includes(word));
}

function classify(text) {
  const normalized = normalize(text);
  for (const [intent, words] of Object.entries(intentWords)) {
    if (includesAny(normalized, words)) return intent;
  }
  if (/^(привет|здравствуйте|добрый|hello|hi)\b/.test(normalized)) return "greeting";
  return "general";
}

function tokenSet(text) {
  return new Set(
    normalize(text)
      .split(" ")
      .filter((word) => word.length > 2 && !stopWords.has(word))
  );
}

function tokenMatches(queryToken, itemToken) {
  if (queryToken === itemToken) return true;
  const min = Math.min(queryToken.length, itemToken.length);
  if (min < 6) return false;
  const prefixLength = Math.min(8, min - 1);
  return queryToken.slice(0, prefixLength) === itemToken.slice(0, prefixLength);
}

function scoreKnowledge(query, itemText) {
  const q = [...tokenSet(query)];
  const item = [...tokenSet(itemText)];
  let score = 0;
  for (const token of q) {
    if (item.some((itemToken) => tokenMatches(token, itemToken))) score += 1;
  }
  return score;
}

export function retrieveKnowledge(business, text, limit = 4) {
  const faq = (business.faq || []).map((item) => ({
    type: "faq",
    title: item.q,
    body: item.a,
    score: scoreKnowledge(text, `${item.q} ${item.a}`)
  }));
  const catalog = (business.catalog || []).map((item) => ({
    type: "catalog",
    title: item.name,
    body: `${item.price ? `Цена: ${item.price}. ` : ""}${item.description || ""}`,
    score: scoreKnowledge(text, `${item.name} ${item.price || ""} ${item.description || ""}`)
  }));

  return [...faq, ...catalog]
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
}

function extractLeadFields(business, text, user = {}) {
  const normalized = normalize(text);
  const fields = {};
  const phone = text.match(/(?:\+?\d[\d\s().-]{8,}\d)/);
  const handle = text.match(/@[a-zA-Z0-9_]{5,}/);

  if (phone) fields.contact = phone[0].replace(/\s+/g, " ").trim();
  if (handle) fields.telegram = handle[0];
  if (user.username) fields.telegram = `@${user.username}`;
  if (user.first_name || user.last_name) fields.name = [user.first_name, user.last_name].filter(Boolean).join(" ");

  for (const item of business.catalog || []) {
    const name = normalize(item.name);
    const synonyms = serviceSynonyms[name] || [];
    const nameTokens = name.split(" ").filter((token) => token.length > 5);
    const serviceMentioned =
      normalized.includes(name) ||
      includesAny(normalized, synonyms) ||
      nameTokens.some((token) => normalized.split(" ").some((inputToken) => tokenMatches(inputToken, token)));
    if (serviceMentioned) {
      fields.interest = item.name;
      break;
    }
  }

  const timeWords = ["сегодня", "завтра", "утром", "днем", "вечером", "пятниц", "суббот", "воскрес", "понедель", "вторник", "сред", "четверг"];
  if (includesAny(normalized, timeWords) || /\b\d{1,2}[:.]\d{2}\b/.test(normalized)) {
    fields.desiredTime = text.trim();
  }

  const budget = text.match(/(?:до|около|примерно|бюджет)?\s?(\d{2,6})\s?(?:₽|руб|р|k|к)/i);
  if (budget) fields.budget = budget[0].trim();

  return fields;
}

function nextQuestion(business, leadDraft) {
  const fields = leadDraft || {};
  if (!fields.interest) return business.leadQuestions?.[0] || "Какая услуга вас интересует?";
  if (!fields.desiredTime) return business.leadQuestions?.[1] || "Когда вам было бы удобно?";
  if (!fields.contact && !fields.telegram) return business.leadQuestions?.[2] || "Как с вами удобнее связаться?";
  return "";
}

function buildSummary({ business, text, intent, fields, leadDraft }) {
  const merged = { ...(leadDraft || {}), ...(fields || {}) };
  const parts = [
    merged.name ? `Имя: ${merged.name}` : "",
    merged.interest ? `Интерес: ${merged.interest}` : "",
    merged.desiredTime ? `Когда: ${merged.desiredTime}` : "",
    merged.contact ? `Контакт: ${merged.contact}` : "",
    merged.telegram ? `Telegram: ${merged.telegram}` : "",
    merged.budget ? `Бюджет: ${merged.budget}` : "",
    `Последний запрос: ${text}`
  ].filter(Boolean);
  return `${business.name}: ${parts.join("; ")}. Намерение: ${intent}.`;
}

function localReply({ business, conversation, text, user }) {
  const intent = classify(text);
  const knowledge = retrieveKnowledge(business, text);
  const fields = extractLeadFields(business, text, user);
  const leadDraft = { ...(conversation?.leadDraft || {}), ...fields };
  const question = nextQuestion(business, leadDraft);
  const hasContact = Boolean(leadDraft.contact || leadDraft.telegram);
  const isHot = intent === "booking" || (intent === "price" && (leadDraft.interest || hasContact));
  const shouldHandoff = Boolean(
    intent === "complaint" || intent === "risky" || (isHot && hasContact && leadDraft.desiredTime)
  );

  let reply = "";
  if (intent === "greeting") {
    reply = `Здравствуйте! Я ассистент ${business.name}. Подскажу по услугам, ценам и помогу передать заявку администратору. Что вас интересует?`;
  } else if (knowledge.length) {
    reply = knowledge
      .map((item) => `${item.title}: ${item.body}`)
      .join("\n\n");
  } else if (intent === "location") {
    reply = business.address
      ? `Мы находимся здесь: ${business.address}. ${business.workingHours ? `Работаем: ${business.workingHours}.` : ""}`
      : "Точный адрес лучше уточнит администратор. Я могу передать ему ваш вопрос.";
  } else if (intent === "risky") {
    reply = "По такому вопросу лучше ответит специалист. Я передам администратору краткое резюме, чтобы вам не пришлось повторять детали.";
  } else {
    reply = "Понял вас. Я могу подсказать по услугам, ценам и записи, а если вопрос нестандартный — передам администратору.";
  }

  if (shouldHandoff) {
    reply += "\n\nЯ передал заявку администратору. Он свяжется с вами и уточнит детали.";
  } else if (question) {
    reply += `\n\n${question}`;
  } else {
    reply += "\n\nДанные для заявки собраны. Передаю администратору.";
  }

  const score = Math.min(
    100,
    30 +
      (leadDraft.interest ? 20 : 0) +
      (leadDraft.desiredTime ? 20 : 0) +
      (hasContact ? 20 : 0) +
      (intent === "booking" ? 20 : 0) +
      (intent === "complaint" ? 15 : 0)
  );

  return {
    intent,
    reply,
    fields,
    leadDraft,
    shouldCreateLead: score >= 55 || shouldHandoff || intent === "price" || intent === "booking",
    shouldHandoff,
    leadPatch: {
      fields: leadDraft,
      score,
      status: shouldHandoff ? "handoff" : score >= 70 ? "hot" : "new",
      summary: buildSummary({ business, text, intent, fields, leadDraft }),
      handoffReason: shouldHandoff ? business.handoffPolicy : ""
    },
    knowledge
  };
}

async function openAiReply({ business, conversation, text, user, llm }) {
  const apiKey = llm?.apiKey;
  if (!apiKey) return null;

  const local = localReply({ business, conversation, text, user });
  const knowledge = [
    ...(business.catalog || []).map((item) => `Услуга: ${item.name}; цена: ${item.price || "не указана"}; описание: ${item.description || ""}`),
    ...(business.faq || []).map((item) => `FAQ: ${item.q} -> ${item.a}`)
  ].join("\n");
  const history = (conversation?.messages || [])
    .slice(-8)
    .map((message) => `${message.role}: ${message.text}`)
    .join("\n");

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${apiKey}`,
    ...(llm?.headers || {})
  };

  const response = await fetch(llm.baseUrl, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: llm.model,
      temperature: 0.2,
      messages: [
        {
          role: "system",
          content:
            "Ты Telegram AI-продавец малого бизнеса. Отвечай кратко, по-русски, только на основе базы знаний. Не выдумывай цены, гарантии, диагнозы или юридические выводы. Если данных не хватает, задай один уточняющий вопрос или предложи передать администратору."
        },
        {
          role: "user",
          content: [
            `Бизнес: ${business.name}`,
            `Ниша: ${business.niche}`,
            `Тон: ${business.tone}`,
            `Рабочие часы: ${business.workingHours}`,
            `Адрес: ${business.address || "не указан"}`,
            `Правило передачи человеку: ${business.handoffPolicy}`,
            "",
            "База знаний:",
            knowledge || "Пока пусто.",
            "",
            "История:",
            history || "Нет истории.",
            "",
            `Новое сообщение клиента: ${text}`,
            "",
            `Локальная политика предлагает следующий CTA: ${local.shouldHandoff ? "передать человеку" : nextQuestion(business, local.leadDraft) || "подтвердить заявку"}`
          ].join("\n")
        }
      ]
    })
  });

  if (!response.ok) return null;
  const data = await response.json();
  const aiText = data.choices?.[0]?.message?.content?.trim();
  if (!aiText) return null;
  return { ...local, reply: aiText };
}

export async function generateSalesReply({ business, conversation, text, user = {}, openai = {} }) {
  const local = localReply({ business, conversation, text, user });
  try {
    const ai = await openAiReply({
      business,
      conversation,
      text,
      user,
      llm: openai
    });
    return ai || local;
  } catch {
    return local;
  }
}

export const internals = {
  classify,
  extractLeadFields,
  localReply,
  normalize,
  nextQuestion
};
