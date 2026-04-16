import crypto from "node:crypto";

function keyFromSecret(secret) {
  if (!secret) return null;
  return crypto.createHash("sha256").update(secret).digest();
}

export function seal(value, secret) {
  if (!value) return null;
  const key = keyFromSecret(secret);
  if (!key) return { mode: "plain", value };

  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    mode: "aes-256-gcm",
    iv: iv.toString("base64"),
    tag: tag.toString("base64"),
    value: encrypted.toString("base64")
  };
}

export function open(sealed, secret) {
  if (!sealed) return "";
  if (typeof sealed === "string") return sealed;
  if (sealed.mode === "plain") return sealed.value || "";

  const key = keyFromSecret(secret);
  if (!key) throw new Error("ENCRYPTION_KEY is required to decrypt managed bot token");

  const decipher = crypto.createDecipheriv(
    "aes-256-gcm",
    key,
    Buffer.from(sealed.iv, "base64")
  );
  decipher.setAuthTag(Buffer.from(sealed.tag, "base64"));
  const decrypted = Buffer.concat([
    decipher.update(Buffer.from(sealed.value, "base64")),
    decipher.final()
  ]);
  return decrypted.toString("utf8");
}
