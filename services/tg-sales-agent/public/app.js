const state = {
  businesses: [],
  business: null,
  dashboard: null,
  bots: [],
  leads: []
};

const $ = (selector) => document.querySelector(selector);
const businessSelect = $("#businessSelect");
const businessForm = $("#businessForm");
const knowledgeForm = $("#knowledgeForm");
const chatLog = $("#chatLog");

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2400);
}

function linesToFaq(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [q, ...a] = line.split("|");
      return { q: q?.trim() || "", a: a.join("|").trim() || "" };
    })
    .filter((item) => item.q && item.a);
}

function linesToCatalog(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [name, price, ...description] = line.split("|");
      return {
        name: name?.trim() || "",
        price: price?.trim() || "",
        description: description.join("|").trim() || ""
      };
    })
    .filter((item) => item.name);
}

function faqToLines(faq = []) {
  return faq.map((item) => `${item.q} | ${item.a}`).join("\n");
}

function catalogToLines(catalog = []) {
  return catalog.map((item) => `${item.name} | ${item.price || ""} | ${item.description || ""}`).join("\n");
}

function setForm(form, values) {
  for (const element of form.elements) {
    if (!element.name) continue;
    if (values[element.name] == null) continue;
    element.value = values[element.name];
  }
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function renderBusinessSelect() {
  businessSelect.innerHTML = state.businesses
    .map((business) => `<option value="${business.id}">${business.name}</option>`)
    .join("");
  if (state.business) businessSelect.value = state.business.id;
}

function renderBusiness() {
  const business = state.business;
  if (!business) return;
  $("#businessTitle").textContent = business.name;
  setForm(businessForm, business);
  knowledgeForm.elements.faq.value = faqToLines(business.faq);
  knowledgeForm.elements.catalog.value = catalogToLines(business.catalog);
}

function renderMetrics() {
  const dashboard = state.dashboard || {};
  $("#metricLeads").textContent = dashboard.leads || 0;
  $("#metricHot").textContent = dashboard.hotLeads || 0;
  $("#metricDialogs").textContent = dashboard.conversations || 0;
  $("#metricHandoffs").textContent = dashboard.handoffs || 0;
}

function renderBots() {
  const list = $("#botsList");
  if (!state.bots.length) {
    list.innerHTML = `<div class="list-item"><strong>Пока нет managed bot</strong><small>Создайте бота через кнопку вверху или через Telegram manager-bot.</small></div>`;
    return;
  }
  list.innerHTML = state.bots
    .map(
      (bot) => `
        <div class="list-item">
          <strong>@${bot.username || bot.telegramBotId}</strong>
          <small>${bot.firstName || "Managed bot"} · ${bot.status} · webhook: ${bot.webhookUrl || "не задан"}</small>
        </div>
      `
    )
    .join("");
}

function renderLeads() {
  const table = $("#leadsTable");
  if (!state.leads.length) {
    table.innerHTML = `<tr><td colspan="6">Лидов пока нет. Отправьте тестовое сообщение в симуляторе.</td></tr>`;
    return;
  }
  table.innerHTML = state.leads
    .map((lead) => {
      const fields = lead.fields || {};
      const client = fields.name || fields.telegram || fields.contact || lead.user?.username || "Клиент";
      return `
        <tr>
          <td><span class="status ${lead.status}">${lead.status}</span></td>
          <td>${lead.score}</td>
          <td>${client}</td>
          <td>${fields.interest || "—"}</td>
          <td>${lead.summary || "—"}</td>
          <td>${new Date(lead.updatedAt).toLocaleString("ru-RU")}</td>
        </tr>
      `;
    })
    .join("");
}

function addBubble(role, text) {
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  node.textContent = text;
  chatLog.appendChild(node);
  chatLog.scrollTop = chatLog.scrollHeight;
}

async function ensureBusiness() {
  const data = await api("/api/businesses");
  state.businesses = data.businesses;
  if (!state.businesses.length) {
    const created = await api("/api/businesses", {
      method: "POST",
      body: JSON.stringify({
        name: "Demo Beauty Studio",
        niche: "cosmetology",
        address: "Москва, центр",
        catalog: [
          {
            name: "Консультация косметолога",
            price: "1500 ₽",
            description: "30 минут, подбор процедуры и рекомендаций."
          },
          {
            name: "Массаж лица",
            price: "от 3500 ₽",
            description: "Ручная техника, 45-60 минут."
          }
        ],
        faq: [
          {
            q: "Можно ли записаться на завтра?",
            a: "Да, напишите удобное время. Администратор подтвердит свободное окно."
          },
          {
            q: "Какая цена консультации?",
            a: "Консультация косметолога стоит 1500 ₽ и длится около 30 минут."
          }
        ]
      })
    });
    state.businesses = [created.business];
  }
  const requested = new URL(location.href).searchParams.get("business");
  state.business = state.businesses.find((business) => business.id === requested) || state.businesses[0];
}

async function loadBusiness(businessId = state.business?.id) {
  const [businessData, dashboardData, botsData, leadsData, linkData] = await Promise.all([
    api(`/api/businesses/${businessId}`),
    api(`/api/dashboard?businessId=${encodeURIComponent(businessId)}`),
    api(`/api/bots?businessId=${encodeURIComponent(businessId)}`),
    api(`/api/leads?businessId=${encodeURIComponent(businessId)}`),
    api(`/api/newbot-link?businessId=${encodeURIComponent(businessId)}`)
  ]);
  state.business = businessData.business;
  state.dashboard = dashboardData.dashboard;
  state.bots = botsData.bots;
  state.leads = leadsData.leads;
  $("#newBotLink").href = linkData.link;
  renderBusinessSelect();
  renderBusiness();
  renderMetrics();
  renderBots();
  renderLeads();
}

async function loadManagerStatus() {
  const data = await api("/api/telegram/manager-status");
  const manager = data.manager || {};
  const text = !manager.configured
    ? "Manager bot пока не подключен."
    : manager.error
      ? `Ошибка Telegram API: ${manager.error}`
      : manager.canManageBots
        ? `Подключен @${manager.username}. Bot Management Mode включен, polling/webhook готов.`
        : `Подключен @${manager.username}, но Bot Management Mode выключен. Включите его в BotFather Mini App, иначе managed bots не создаются.`;
  $("#managerStatusText").textContent = text;
}

businessSelect.addEventListener("change", async () => {
  await loadBusiness(businessSelect.value);
});

$("#createBusiness").addEventListener("click", async () => {
  const created = await api("/api/businesses", {
    method: "POST",
    body: JSON.stringify({ name: "Новый AI-продавец", niche: "service_business" })
  });
  state.businesses.push(created.business);
  await loadBusiness(created.business.id);
  toast("Бизнес создан");
});

businessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = formData(businessForm);
  const data = await api(`/api/businesses/${state.business.id}`, {
    method: "PUT",
    body: JSON.stringify(patch)
  });
  state.business = data.business;
  state.businesses = state.businesses.map((business) => (business.id === data.business.id ? data.business : business));
  renderBusinessSelect();
  renderBusiness();
  toast("Настройки сохранены");
});

knowledgeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = {
    faq: linesToFaq(knowledgeForm.elements.faq.value),
    catalog: linesToCatalog(knowledgeForm.elements.catalog.value)
  };
  const data = await api(`/api/businesses/${state.business.id}`, {
    method: "PUT",
    body: JSON.stringify(patch)
  });
  state.business = data.business;
  renderBusiness();
  toast("База знаний обновлена");
});

$("#simulateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = event.currentTarget.elements.text;
  const text = input.value.trim();
  if (!text) return;
  addBubble("user", text);
  input.value = "";
  const data = await api("/api/simulate", {
    method: "POST",
    body: JSON.stringify({ businessId: state.business.id, text })
  });
  addBubble("assistant", data.reply);
  await loadBusiness(state.business.id);
});

await ensureBusiness();
await loadBusiness(state.business.id);
await loadManagerStatus();
addBubble("assistant", "Напишите тестовый вопрос клиента. Например: «Сколько стоит консультация и можно ли завтра вечером?»");
