import test from "node:test";
import assert from "node:assert/strict";
import { generateSalesReply, retrieveKnowledge, internals } from "../src/agent.js";

const business = {
  name: "Demo Beauty Studio",
  niche: "cosmetology",
  tone: "friendly_expert",
  address: "Москва, Цветной бульвар",
  workingHours: "10:00-20:00",
  handoffPolicy: "Передавать человеку горячие заявки и медицинские вопросы.",
  catalog: [
    {
      name: "Консультация косметолога",
      price: "1500 ₽",
      description: "30 минут, подбор процедуры."
    }
  ],
  faq: [
    {
      q: "Какая цена консультации?",
      a: "Консультация косметолога стоит 1500 ₽."
    }
  ],
  leadQuestions: ["Какая услуга интересует?", "Когда удобно?", "Как с вами связаться?"]
};

test("retrieves relevant FAQ and catalog items", () => {
  const found = retrieveKnowledge(business, "сколько стоит консультация косметолога?");
  assert.equal(found[0].type, "faq");
  assert.match(found.map((item) => item.body).join(" "), /1500/);
});

test("classifies booking intent", () => {
  assert.equal(internals.classify("Хочу записаться завтра вечером"), "booking");
});

test("extracts a hot lead from a booking message", async () => {
  const result = await generateSalesReply({
    business,
    conversation: { leadDraft: {}, messages: [] },
    text: "Хочу записаться завтра вечером на консультацию, мой телефон +7 999 111-22-33",
    user: { first_name: "Анна", username: "anna_demo" }
  });

  assert.equal(result.shouldCreateLead, true);
  assert.equal(result.shouldHandoff, true);
  assert.equal(result.leadPatch.status, "handoff");
  assert.match(result.leadPatch.fields.contact, /\+7 999/);
  assert.match(result.reply, /администратор/i);
});

test("does not invent a risky medical answer", async () => {
  const result = await generateSalesReply({
    business,
    conversation: { leadDraft: {}, messages: [] },
    text: "У меня противопоказания, какой диагноз и лечение?",
    user: {}
  });

  assert.equal(result.shouldHandoff, true);
  assert.match(result.reply, /специалист|администратор/i);
});
