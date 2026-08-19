// The Telegram chat: /start, station pick, profile pick, feedback tap.
//
// This Worker takes taps and writes rows. It contains no breakpoint table, no
// sub-index arithmetic and no message wording — all of that is Python, in
// scripts/aqi.py and scripts/send_alerts.py, next to its tests and next to
// where Phase 4 needs the identical numbers.
//
// It holds no secret in this file. TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
// and DATABASE_URL are encrypted Worker secrets (`wrangler secret put`).
//
// Privacy, build plan §5: the only thing stored about a person is their chat
// id, their station and their profile. Never a name, a username, coordinates,
// or anything they type. Nothing here logs message text.

import { neon } from "@neondatabase/serverless";

// Telegram caps callback_data at 64 bytes, which is why every id below travels
// as a short prefix and a number rather than as JSON.
const CB_STATION = "st";
const CB_DARK = "dark";
const CB_PROFILE = "pr";
const CB_FEEDBACK = "fb";

const DISCLAIMER =
  "This is not medical advice. Every health sentence in these messages is " +
  "quoted from CPCB's own published AQI advisory and cited to the document " +
  "it came from. Air quality data © Central Pollution Control Board, via " +
  "data.gov.in.";

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Display only. The stored station_name is the exact CPCB string, byte for
// byte, including the trailing space on 'Sector-6, Panchkula - HSPCB ' and the
// double space in the Dharuhera one — two sources join on it exactly, so it is
// never trimmed anywhere it matters.
const shortName = (name) => name.replace(/\s*-\s*(HSPCB|IITM)\s*$/, "").trim();

async function tg(env, method, payload) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) {
    // The body carries Telegram's own description, which is the difference
    // between "token is wrong" and "this one user blocked the bot". Never
    // swallowed (build plan §0.5) — but the token is in the URL, so nothing
    // that could quote it is included in the message.
    throw new Error(`telegram ${method} failed: HTTP ${res.status} ${await res.text()}`);
  }
  return (await res.json()).result;
}

const send = (env, chatId, text, replyMarkup) =>
  tg(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
  });

// Telegram shows a spinner on a tapped button until this is answered.
const answer = (env, id, text) =>
  tg(env, "answerCallbackQuery", {
    callback_query_id: id,
    ...(text ? { text, show_alert: false } : {}),
  });

// Liveness is derived, hour to hour, and is NOT stations.is_active. That flag
// is a hand-set config fact meaning "the ingester should expect this station";
// nothing in the repo ever sets it FALSE, and flipping it would stop ingesting
// the station rather than hide it here. What matters for a subscriber is
// whether the newest bulletin carries a real PM2.5 number for it.
//
// The newest bulletin only, not a 3-bulletin window: measured 2026-08-13 across
// 66 bulletins and 30 stations, sensor outages come in blocks of 10 to 19
// bulletins and not one station was dark for exactly one. A lookback would
// guard a failure mode that does not occur.
async function stationList(sql) {
  return sql`
    SELECT s.station_id, s.station_name, s.city,
           (o.value_avg IS NOT NULL) AS live
    FROM stations s
    LEFT JOIN observations o
      ON o.station_id = s.station_id
     AND o.pollutant_id = 'PM2.5'
     AND o.observation_ts = (SELECT max(observation_ts) FROM observations)
    WHERE s.is_active
    ORDER BY s.city, s.station_name
  `;
}

// Dark stations are shown, marked, rather than hidden. Hiding one leaves a
// person hunting for a city that was there yesterday and concluding the bot is
// broken; five of thirty stations flickered during Phase 1, four of which came
// back on their own.
function stationKeyboard(stations) {
  return {
    inline_keyboard: stations.map((s) => [
      s.live
        ? { text: shortName(s.station_name), callback_data: `${CB_STATION}:${s.station_id}` }
        : {
            text: `${shortName(s.station_name)} — no data`,
            callback_data: `${CB_DARK}:${s.station_id}`,
          },
    ]),
  };
}

// Read from the table, never hardcoded. Build plan §1: adding a fourth profile
// must be a row insert, not a refactor — and this keyboard is where that
// promise is either kept or quietly broken.
function profileKeyboard(profiles, stationId) {
  return {
    inline_keyboard: profiles.map((p) => [
      { text: p.label, callback_data: `${CB_PROFILE}:${stationId}:${p.profile_id}` },
    ]),
  };
}

async function onCommand(env, sql, chatId, text) {
  const command = text.trim().split(/[\s@]/)[0].toLowerCase();

  if (command === "/start" || command === "/stations") {
    // /start also clears a pause. send_alerts.py pauses a subscriber whose
    // chat answered 403, so this is the documented way back in.
    await sql`UPDATE subscribers SET is_paused = FALSE WHERE chat_id = ${chatId}`;
    const stations = await stationList(sql);
    const live = stations.filter((s) => s.live).length;
    await send(
      env,
      chatId,
      `<b>Vayu Haryana Air Alert</b>\n\n` +
        `One message a day at 7:00 AM IST: the PM2.5 at your station, and CPCB's ` +
        `own health statement for the air quality band it falls in.\n\n` +
        `${DISCLAIMER}\n\n` +
        `Pick your station (${live} of ${stations.length} reporting right now):`,
      stationKeyboard(stations),
    );
    return;
  }

  if (command === "/about") {
    await send(
      env,
      chatId,
      `<b>What this is</b>\n` +
        `Station-level PM2.5 for Haryana, from CPCB's hourly feed via ` +
        `data.gov.in, delivered once a day at 7:00 AM IST.\n\n` +
        `<b>What the number means</b>\n` +
        `The headline is PM2.5 in µg/m³. The health sentence is selected by the ` +
        `overall AQI — the worst pollutant's sub-index — because that is the ` +
        `band CPCB wrote those sentences against. When fewer than three ` +
        `pollutants are reporting, CPCB's own rule says no AQI can be computed, ` +
        `and the message says so instead of guessing.\n\n` +
        `<b>What is stored about you</b>\n` +
        `Your Telegram chat id, your station, and your profile. Nothing else — ` +
        `no name, no location, no health record. /stop deletes the row.\n\n` +
        `<b>Commands</b>\n` +
        `/stations — change station\n` +
        `/pause — stop the daily message, keep the subscription\n` +
        `/stop — delete the subscription\n\n` +
        `${DISCLAIMER}`,
    );
    return;
  }

  if (command === "/pause") {
    const rows = await sql`
      UPDATE subscribers SET is_paused = TRUE WHERE chat_id = ${chatId}
      RETURNING chat_id
    `;
    await send(
      env,
      chatId,
      rows.length
        ? "Paused. No daily message until you send /start."
        : "You are not subscribed. Send /start to set it up.",
    );
    return;
  }

  if (command === "/stop") {
    // Deleted, not flagged. Someone asking to be removed is removed — build
    // plan §5's data minimisation is the whole reason there is so little to
    // delete. Their past feedback rows survive because feedback.chat_id is
    // deliberately not a foreign key: the record that a message was once
    // useful is the retention measurement, and it names nobody.
    const rows = await sql`
      DELETE FROM subscribers WHERE chat_id = ${chatId} RETURNING chat_id
    `;
    await send(
      env,
      chatId,
      rows.length
        ? "Subscription deleted. Send /start any time to set it up again."
        : "You were not subscribed.",
    );
    return;
  }

  await send(
    env,
    chatId,
    "Commands: /start, /stations, /about, /pause, /stop",
  );
}

async function onCallback(env, sql, query) {
  const chatId = query.from.id;
  const [kind, a, b] = String(query.data || "").split(":");

  if (kind === CB_DARK) {
    await answer(
      env,
      query.id,
      "That station's PM2.5 sensor is not reporting right now. Outages usually last a few hours.",
    );
    return;
  }

  if (kind === CB_STATION) {
    const profiles = await sql`
      SELECT profile_id, label, description FROM profiles ORDER BY profile_id
    `;
    const stations = await sql`
      SELECT station_name FROM stations WHERE station_id = ${Number(a)}
    `;
    if (!stations.length) {
      await answer(env, query.id, "That station no longer exists. Send /stations.");
      return;
    }
    await answer(env, query.id);
    await send(
      env,
      chatId,
      `<b>${esc(shortName(stations[0].station_name))}</b>\n\nWho is this for?\n\n` +
        profiles.map((p) => `• <b>${esc(p.label)}</b> — ${esc(p.description)}`).join("\n"),
      profileKeyboard(profiles, Number(a)),
    );
    return;
  }

  if (kind === CB_PROFILE) {
    // Looked up before the insert, so a station retired between the keyboard
    // being drawn and the button being tapped answers a sentence rather than
    // raising a foreign-key error — which, because a non-2xx makes Telegram
    // redeliver, would retry forever.
    const stations = await sql`
      SELECT station_name FROM stations WHERE station_id = ${Number(a)}
    `;
    if (!stations.length) {
      await answer(env, query.id, "That station no longer exists. Send /stations.");
      return;
    }
    await sql`
      INSERT INTO subscribers (chat_id, station_id, profile_id, is_paused)
      VALUES (${chatId}, ${Number(a)}, ${b}, FALSE)
      ON CONFLICT (chat_id) DO UPDATE SET
        station_id = EXCLUDED.station_id,
        profile_id = EXCLUDED.profile_id,
        is_paused  = FALSE
    `;
    // Fired before the confirmation, not after, and awaited. If the dispatch
    // fails this handler throws, Telegram redelivers the same tap, and the
    // upsert above runs again harmlessly — so the retry costs a repeated
    // spinner rather than a duplicate "Subscribed" message.
    //
    // Today's message, not an extra one: send_alerts.py skips anyone already
    // holding a sent_log row for today, and sent_log's UNIQUE (chat_id,
    // send_date) is the backstop if two runs land together.
    await env.TRIGGER.fetch("https://trigger/send", { method: "POST" });

    await answer(env, query.id, "Subscribed");
    await send(
      env,
      chatId,
      `Subscribed to <b>${esc(shortName(stations[0].station_name))}</b>.\n\n` +
        `Today's reading is on its way — it takes a minute or two. After that, ` +
        `one message every morning at 7:00 AM IST. Change station with ` +
        `/stations, stop with /stop.`,
    );
    return;
  }

  if (kind === CB_FEEDBACK) {
    // The ownership check, and it is not optional. callback_data is echoed
    // back by the client, so without `AND chat_id = <the tapper>` anyone could
    // rate someone else's message by sending a forged id — and Gate 2 is
    // counted from this table.
    const rating = b === "-1" ? -1 : 1;
    const rows = await sql`
      INSERT INTO feedback (sent_log_id, chat_id, rating, station_id, pm25_value)
      SELECT id, chat_id, ${rating}, station_id, pm25_value
      FROM sent_log WHERE id = ${Number(a)} AND chat_id = ${chatId}
      ON CONFLICT (sent_log_id, chat_id) DO UPDATE SET
        rating = EXCLUDED.rating, created_at = now()
      RETURNING id
    `;
    await answer(
      env,
      query.id,
      rows.length ? "Thanks — noted." : "That message is not yours to rate.",
    );
    return;
  }

  await answer(env, query.id);
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("not found", { status: 404 });

    // Before parsing the body, not after. This URL is public — that is what a
    // webhook is — so this header is the only thing standing between a
    // stranger and the ability to create subscriptions and forge feedback
    // taps. Telegram sends it on every delivery because setWebhook was called
    // with secret_token.
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("unauthorized", { status: 401 });
    }

    const update = await request.json();
    const sql = neon(env.DATABASE_URL);

    // A non-2xx makes Telegram redeliver the update. That is wanted: every
    // write above is an upsert or a delete keyed on the chat id, so a repeat
    // is a no-op, and a failure that Telegram forgets about is a failure
    // nobody ever sees. Cloudflare's error metrics show it too.
    if (update.message?.text) {
      await onCommand(env, sql, update.message.chat.id, update.message.text);
    } else if (update.callback_query) {
      await onCallback(env, sql, update.callback_query);
    }

    return new Response("ok");
  },
};
