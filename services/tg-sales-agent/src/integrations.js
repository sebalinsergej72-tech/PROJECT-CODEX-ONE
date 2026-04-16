export async function postJson(url, payload, timeoutMs = 6000) {
  if (!url) return { skipped: true };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    return { ok: response.ok, status: response.status, text: await response.text().catch(() => "") };
  } finally {
    clearTimeout(timeout);
  }
}

export async function syncLead({ business, lead }) {
  const payload = {
    type: "lead",
    business: { id: business.id, name: business.name, niche: business.niche },
    lead
  };
  const [sheet, crm] = await Promise.allSettled([
    postJson(business.sheetWebhookUrl, payload),
    postJson(business.crmWebhookUrl, payload)
  ]);
  return { sheet, crm };
}
