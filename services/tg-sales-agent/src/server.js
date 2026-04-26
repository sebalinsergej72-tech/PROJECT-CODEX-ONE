import http from "node:http";
import { config } from "./config.js";
import { handleRequest } from "./http.js";
import { getManagerStatus, startPolling } from "./bot-runner.js";

const server = http.createServer(handleRequest);

server.listen(config.port, async () => {
  console.log(`TG Sales Agent listening on http://localhost:${config.port}`);
  console.log(`Admin UI: http://localhost:${config.port}`);
  const managerStatus = await getManagerStatus();
  if (managerStatus.configured) {
    console.log(
      `Manager bot: @${managerStatus.username || config.managerBotUsername} · can_manage_bots=${String(managerStatus.canManageBots)}`
    );
  }
  if (config.telegramPolling) {
    await startPolling();
    console.log("Telegram polling enabled");
  }
});
