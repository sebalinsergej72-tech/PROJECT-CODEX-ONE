import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { JsonStore } from "../src/storage.js";

function tmpStore() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-sales-agent-"));
  return new JsonStore(path.join(dir, "db.json"));
}

test("creates business with starter knowledge", () => {
  const store = tmpStore();
  const business = store.createBusiness({ name: "Studio" });
  assert.equal(business.name, "Studio");
  assert.ok(business.faq.length > 0);
  assert.ok(business.catalog.length > 0);
});

test("upserts conversation and lead", () => {
  const store = tmpStore();
  const business = store.createBusiness({ name: "Studio" });
  const conversation = store.upsertConversation({
    businessId: business.id,
    botId: "bot_1",
    chatId: 100,
    user: { username: "client" },
    message: "хочу записаться",
    role: "user"
  });
  const lead = store.upsertLead({
    businessId: business.id,
    botId: "bot_1",
    conversationId: conversation.id,
    user: { username: "client" },
    patch: {
      fields: { interest: "Консультация" },
      score: 80,
      status: "hot",
      summary: "Готов записаться"
    }
  });

  assert.equal(lead.status, "hot");
  assert.equal(store.listLeads(business.id).length, 1);
  assert.equal(store.dashboard(business.id).hotLeads, 1);
});
