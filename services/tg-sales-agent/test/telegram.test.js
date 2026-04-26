import test from "node:test";
import assert from "node:assert/strict";
import { newManagedBotLink } from "../src/telegram.js";

test("builds Telegram managed bot creation link", () => {
  const link = newManagedBotLink({
    managerUsername: "ManagerBot",
    suggestedUsername: "StudioAgentBot",
    suggestedName: "Studio Agent"
  });
  assert.equal(link, "https://t.me/newbot/ManagerBot/StudioAgentBot?name=Studio%20Agent");
});
