/**
 * SUI-Quant V1.0 — External Dead-Man's Switch (Cloudflare Worker + KV + Cron Trigger)
 *
 * Deployment Location : Cloudflare Edge Global Network (Outside Tencent Cloud Tokyo)
 * Runtime Entity      : Cloudflare Workers Isolate (`sui-quant-dms-worker`)
 * Independent Timer   : Cron Trigger `*/1 * * * *` (fires every 60 seconds regardless of server state)
 * Storage             : Cloudflare Workers KV (`SUI_QUANT_DMS_KV`)
 * Alert Egress        : FCM HTTP v1 / Telegram Bot API / Webhook (`ALERT_WEBHOOK_URL`)
 *
 * Status in Task A    : Code & Quota Accounting complete; Production deployment marked
 *                       PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS (zero fabricated live SaaS receipts).
 */

const SILENCE_TIMEOUT_MS = 180 * 1000; // 180s independent silence threshold
const ALERT_COOLDOWN_MS = 900 * 1000;  // 15m repeat suppression window

async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function dispatchExternalAlert(env, incidentType, payload) {
  const alertBody = {
    alert_source: "CLOUDFLARE_EXTERNAL_DMS_WORKER",
    incident_type: incidentType,
    monitored_host: "<JAPAN_CLOUD_IP_REDACTED> (Tokyo-2vCPU-2GB)",
    evaluated_at_utc: new Date().toISOString(),
    details: payload,
  };
  if (!env.ALERT_WEBHOOK_URL) {
    return { delivered: false, reason: "MISSING_ALERT_WEBHOOK_URL" };
  }
  const resp = await fetch(env.ALERT_WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(alertBody),
  });
  return {
    delivered: resp.ok,
    http_status: resp.status,
    receipt_text: await resp.text(),
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/beacon") {
      const body = await request.json();
      const canonical =
        `${body.boot_id}|${body.sent_at_utc}|${body.trading_state}|` +
        `${body.r01_healthy ? "1" : "0"}|${body.manifest_root_sha256}`;
      const expectedMac = await hmacSha256Hex(env.BEACON_WRITE_TOKEN, canonical);
      if (expectedMac !== body.hmac_sha256) {
        return new Response(JSON.stringify({ ok: false, error: "INVALID_HMAC" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }
      const record = {
        ...body,
        received_at_ms: Date.now(),
        received_at_utc: new Date().toISOString(),
      };
      await env.SUI_QUANT_DMS_KV.put("latest_beacon", JSON.stringify(record));
      return new Response(
        JSON.stringify({ ok: true, received_at_utc: record.received_at_utc }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
    return new Response("Not Found", { status: 404 });
  },

  async scheduled(event, env, ctx) {
    const nowMs = Date.now();
    const raw = await env.SUI_QUANT_DMS_KV.get("latest_beacon");
    if (!raw) {
      await dispatchExternalAlert(env, "EXTERNAL_NO_BEACON_REGISTERED", { now_ms: nowMs });
      return;
    }
    const beacon = JSON.parse(raw);
    const silenceMs = nowMs - beacon.received_at_ms;

    if (silenceMs > SILENCE_TIMEOUT_MS) {
      await dispatchExternalAlert(env, "EXTERNAL_HEARTBEAT_TIMEOUT", {
        silence_seconds: Math.round(silenceMs / 1000),
        last_seen_utc: beacon.received_at_utc,
        last_boot_id: beacon.boot_id,
      });
      return;
    }

    if (beacon.manifest_root_sha256 !== env.PINNED_MANIFEST_ROOT_SHA256) {
      await dispatchExternalAlert(env, "EXTERNAL_MANIFEST_ROOT_MISMATCH", {
        reported_root: beacon.manifest_root_sha256,
        expected_root: env.PINNED_MANIFEST_ROOT_SHA256,
      });
    }
  },
};
