// The Telegram chat: language pick, /start, station pick, profile pick,
// feedback tap.
//
// This Worker takes taps and writes rows. It contains no breakpoint table, no
// sub-index arithmetic and no daily-message wording — all of that is Python, in
// scripts/aqi.py and scripts/send_alerts.py, next to its tests and next to
// where Phase 4 needs the identical numbers. What lives here is only the chat
// around that message.
//
// It holds no secret in this file. TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
// and DATABASE_URL are encrypted Worker secrets (`wrangler secret put`).
//
// Privacy, build plan §5: the only thing stored about a person is their chat
// id, their station, their profile and their chosen language. Never a name, a
// username, coordinates, or anything they type. Nothing here logs message text.
//
// Every sentence a person reads is written for someone with no technical
// background and no English degree. "Sub-index", "bulletin", "station" and
// "concentration" are absent on purpose — they name a mechanism rather than a
// fact, and a reader who does not recognise one reads the whole message as
// broken. "AQI", "PM2.5", "PM10" and "µg/m³" stay, for the opposite reason:
// the reader meets all four on every other air app, so an invented plain-word
// synonym ("air score") makes our number look like a different one.

import { neon } from "@neondatabase/serverless";

// Telegram caps callback_data at 64 bytes, which is why every id below travels
// as a short prefix and a number rather than as JSON. The language rides along
// in every payload because a brand-new person has picked one BEFORE they have
// a subscribers row to read it back from.
const CB_LANG = "lg";
const CB_STATION = "st";
const CB_DARK = "dark";
const CB_PROFILE = "pr";
const CB_FEEDBACK = "fb";

// Rolling windows, not calendar day and week. A rolling window needs no
// timezone at all, where a calendar one would have to agree with the IST date
// send_alerts.py computes — a second place for the same decision to drift.
const MAX_CHANGES_DAY = 2;
const MAX_CHANGES_WEEK = 4;

const LANGS = ["en", "hi"];
const langOf = (code) => (LANGS.includes(code) ? code : "en");

// Everything the chat says, in both languages.
//
// The daily message is NOT here — it is composed in Python. Keeping the two
// apart is the same rule that keeps the breakpoint table out of this file: one
// table written in two languages goes stale in one of them.
const T = {
  en: {
    disclaimer:
      "This is not a doctor's advice. The health line in each message is " +
      "copied word for word from the government's own air notice, and we say " +
      "which page it came from. Air data: Central Pollution Control Board, " +
      "via data.gov.in.",
    welcome:
      "🌬️ <b>Vayu — Haryana Air Alert</b>\n\n" +
      "Every morning at 7:00 AM you get one message: how dirty the air is " +
      "where you live, and what the government says about it.\n\n" +
      "That is all. Nothing to open, nothing to pay.",
    pickPlace: "👇 <b>Pick your place</b> ({live} of {total} working right now):",
    noData: "no reading",
    darkPopup:
      "No reading from this place right now. The machine there is down. " +
      "It usually comes back in a few hours.",
    whoFor: "👤 <b>Who is this for?</b>\n\nPick the one closest to you:",
    gone: "That place is gone from the list. Send /stations again.",
    subscribedPopup: "✅ Done",
    subscribed:
      "✅ <b>All set — {name}</b>\n\n" +
      "Today's reading is coming in a minute or two.\n\n" +
      "After that: one message every morning at 7:00 AM.\n\n" +
      "🔄 /stations — change place ({day} a day, {week} a week)\n" +
      "🌐 /language — English / हिंदी\n" +
      "⏸ /pause — stop the daily message\n" +
      "🛑 /stop — remove me completely",
    samePopup: "Already on this one",
    same:
      "You are already set for <b>{name}</b>. Nothing changed.\n\n" +
      "Your next message comes at 7:00 AM.",
    cappedPopup: "Changed — but no reading right now",
    capped:
      "🔄 Changed to <b>{name}</b>.\n\n" +
      "You can change place {day} times a day and {week} times a week. You " +
      "have used them up, so today's reading is not being fetched right now.\n\n" +
      "Your next message comes at 7:00 AM for <b>{name}</b>.",
    about:
      "<b>🌬️ What this bot does</b>\n" +
      "Once a day, at 7:00 AM, it tells you how dirty the air is where you " +
      "live — so you can decide about going out, sending a child to play, or " +
      "wearing a mask.\n\n" +
      "<b>🔢 The two numbers</b>\n" +
      "• <b>Fine dust (PM2.5)</b> — how much tiny dust is in the air this " +
      "hour, in µg/m³. Under 30 is clean. Higher is worse.\n" +
      "• <b>AQI</b> — the government's number out of 500, for the whole last " +
      "day, counting every kind of dirt in the air — the same AQI the other " +
      "air apps show.\n\n" +
      "<b>🔒 What we keep about you</b>\n" +
      "Your chat id, your place, who it is for, and your language. Nothing " +
      "else — no name, no phone, no address, no health record. /stop wipes it.\n\n" +
      "<b>⌨️ Commands</b>\n" +
      "/stations — change place ({day} a day, {week} a week)\n" +
      "/language — English / हिंदी\n" +
      "/pause — stop the daily message, stay signed up\n" +
      "/stop — remove me completely\n\n" +
      "{disclaimer}",
    pausedYes: "⏸ Paused. No daily message until you send /start.",
    pausedNo: "You are not signed up. Send /start to begin.",
    stoppedYes: "🛑 Removed. Everything about you is deleted. Send /start any time to begin again.",
    stoppedNo: "You were not signed up.",
    langPrompt: "🌐 <b>Choose your language</b>\n\nअपनी भाषा चुनें",
    langSetPopup: "✅ English",
    langSet: "✅ Language set to English.",
    ratedPopup: "✅ Noted — thank you",
    thanksUp: "👍 <b>Thank you.</b>\n\nNoted that today's message helped.",
    thanksDown:
      "👎 <b>Thank you for saying so.</b>\n\n" +
      "Noted that today's message did not help. This is how we learn what to " +
      "change.",
    notYours: "That message is not yours to rate.",
    help:
      "⌨️ <b>Commands</b>\n\n" +
      "/start — begin, or start again\n" +
      "/stations — change place\n" +
      "/language — English / हिंदी\n" +
      "/about — what this bot does\n" +
      "/pause — stop the daily message\n" +
      "/stop — remove me completely",
  },
  hi: {
    disclaimer:
      "यह डॉक्टरी सलाह नहीं है। हर संदेश की सेहत वाली लाइन सरकार की अपनी हवा " +
      "रिपोर्ट से हूबहू ली गई है, और हम बताते हैं कि किस पन्ने से। हवा का " +
      "डेटा: Central Pollution Control Board, data.gov.in से।",
    welcome:
      "🌬️ <b>वायु — हरियाणा हवा अलर्ट</b>\n\n" +
      "हर सुबह 7:00 बजे एक संदेश: आपके इलाके की हवा कितनी गंदी है, और सरकार " +
      "उसके बारे में क्या कहती है।\n\n" +
      "बस इतना ही। कुछ खोलना नहीं, कुछ देना नहीं।",
    pickPlace: "👇 <b>अपनी जगह चुनें</b> ({total} में से {live} अभी चालू हैं):",
    noData: "रीडिंग नहीं",
    darkPopup:
      "इस जगह की अभी कोई रीडिंग नहीं है। वहाँ की मशीन बंद है। आम तौर पर कुछ " +
      "घंटों में चालू हो जाती है।",
    whoFor: "👤 <b>यह किसके लिए है?</b>\n\nजो आपके सबसे नज़दीक हो, वह चुनें:",
    gone: "यह जगह लिस्ट से हट गई है। दोबारा /stations भेजें।",
    subscribedPopup: "✅ हो गया",
    subscribed:
      "✅ <b>सब तैयार — {name}</b>\n\n" +
      "आज की रीडिंग एक-दो मिनट में आ रही है।\n\n" +
      "उसके बाद: हर सुबह 7:00 बजे एक संदेश।\n\n" +
      "🔄 /stations — जगह बदलें (दिन में {day} बार, हफ़्ते में {week})\n" +
      "🌐 /language — English / हिंदी\n" +
      "⏸ /pause — रोज़ का संदेश बंद करें\n" +
      "🛑 /stop — मुझे पूरी तरह हटा दें",
    samePopup: "यह पहले से चुनी है",
    same:
      "आप पहले से <b>{name}</b> पर हैं। कुछ नहीं बदला।\n\n" +
      "अगला संदेश सुबह 7:00 बजे आएगा।",
    cappedPopup: "बदल गई — पर अभी रीडिंग नहीं",
    capped:
      "🔄 बदलकर <b>{name}</b> कर दी गई।\n\n" +
      "जगह दिन में {day} बार और हफ़्ते में {week} बार बदल सकते हैं। वे पूरे हो " +
      "गए हैं, इसलिए आज की रीडिंग अभी नहीं मँगाई जा रही।\n\n" +
      "अगला संदेश सुबह 7:00 बजे <b>{name}</b> के लिए आएगा।",
    about:
      "<b>🌬️ यह बॉट क्या करता है</b>\n" +
      "दिन में एक बार, सुबह 7:00 बजे, यह बताता है कि आपके इलाके की हवा कितनी " +
      "गंदी है — ताकि आप तय कर सकें कि बाहर जाना है, बच्चे को खेलने भेजना है, " +
      "या मास्क पहनना है।\n\n" +
      "<b>🔢 दो नंबर</b>\n" +
      "• <b>बारीक धूल (PM2.5)</b> — इस घंटे हवा में कितनी छोटी धूल है, µg/m³ " +
      "में। 30 से कम मतलब साफ़। ज़्यादा मतलब खराब।\n" +
      "• <b>AQI</b> — सरकार का नंबर, 500 में से, पूरे पिछले दिन का, हवा की हर " +
      "तरह की गंदगी को जोड़कर — वही AQI जो बाकी हवा ऐप दिखाते हैं।\n\n" +
      "<b>🔒 आपके बारे में हम क्या रखते हैं</b>\n" +
      "आपकी चैट आईडी, आपकी जगह, यह किसके लिए है, और आपकी भाषा। और कुछ नहीं — " +
      "न नाम, न फ़ोन, न पता, न सेहत का कोई रिकॉर्ड। /stop सब मिटा देता है।\n\n" +
      "<b>⌨️ कमांड</b>\n" +
      "/stations — जगह बदलें (दिन में {day}, हफ़्ते में {week})\n" +
      "/language — English / हिंदी\n" +
      "/pause — रोज़ का संदेश बंद करें, सब्सक्रिप्शन चालू रखें\n" +
      "/stop — मुझे पूरी तरह हटा दें\n\n" +
      "{disclaimer}",
    pausedYes: "⏸ रोक दिया। /start भेजने तक कोई रोज़ का संदेश नहीं आएगा।",
    pausedNo: "आप साइन अप नहीं हैं। शुरू करने के लिए /start भेजें।",
    stoppedYes: "🛑 हटा दिया। आपके बारे में सब कुछ मिटा दिया गया। दोबारा शुरू करने के लिए कभी भी /start भेजें।",
    stoppedNo: "आप साइन अप नहीं थे।",
    langPrompt: "🌐 <b>अपनी भाषा चुनें</b>\n\nChoose your language",
    langSetPopup: "✅ हिंदी",
    langSet: "✅ भाषा हिंदी कर दी गई।",
    ratedPopup: "✅ दर्ज हो गया — धन्यवाद",
    thanksUp: "👍 <b>धन्यवाद।</b>\n\nदर्ज कर लिया कि आज का संदेश काम आया।",
    thanksDown:
      "👎 <b>बताने के लिए धन्यवाद।</b>\n\n" +
      "दर्ज कर लिया कि आज का संदेश काम नहीं आया। इसी से हमें पता चलता है कि " +
      "क्या बदलना है।",
    notYours: "यह संदेश आपका नहीं है, इसलिए आप इसे रेट नहीं कर सकते।",
    help:
      "⌨️ <b>कमांड</b>\n\n" +
      "/start — शुरू करें, या दोबारा शुरू करें\n" +
      "/stations — जगह बदलें\n" +
      "/language — English / हिंदी\n" +
      "/about — यह बॉट क्या करता है\n" +
      "/pause — रोज़ का संदेश बंद करें\n" +
      "/stop — मुझे पूरी तरह हटा दें",
  },
};

// Minimal {name} substitution. Deliberately not a template engine: there are
// four placeholders in the whole file and every value is either a number
// constant or a station name that esc() has already been through.
const fill = (s, vars = {}) =>
  s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Display only. The stored station_name is the exact CPCB string, byte for
// byte, including the trailing space on 'Sector-6, Panchkula - HSPCB ' and the
// double space in the Dharuhera one — two sources join on it exactly, so it is
// never trimmed anywhere it matters.
//
// "- HSPCB" is the name of the state pollution board and means nothing to a
// person choosing where they live, so it is stripped from every button and
// every sentence they read.
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

const langOfChat = async (sql, chatId) => {
  const rows = await sql`SELECT lang FROM subscribers WHERE chat_id = ${chatId}`;
  return rows.length ? langOf(rows[0].lang) : null;
};

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
//
// One button per row. Two columns fitted more names on a screen and Telegram
// truncated the longer half of them with an ellipsis, so a person picking a
// place could not read which place it was. A full-width button is the only
// thing that guarantees the whole name, and a scroll beats a guess.
function stationKeyboard(stations, lang) {
  const buttons = stations.map((s) =>
    s.live
      ? {
          text: shortName(s.station_name),
          callback_data: `${CB_STATION}:${lang}:${s.station_id}`,
        }
      : {
          text: `⚪ ${shortName(s.station_name)} — ${T[lang].noData}`,
          callback_data: `${CB_DARK}:${s.station_id}`,
        },
  );
  return { inline_keyboard: buttons.map((b) => [b]) };
}

// Read from the table, never hardcoded. Build plan §1: adding a fourth profile
// must be a row insert, not a refactor — and this keyboard is where that
// promise is either kept or quietly broken. COALESCE, so a profile added
// without a Hindi label still gets a working button.
function profileKeyboard(profiles, stationId, lang) {
  return {
    inline_keyboard: profiles.map((p) => [
      {
        text: p.label,
        callback_data: `${CB_PROFILE}:${lang}:${stationId}:${p.profile_id}`,
      },
    ]),
  };
}

const languageKeyboard = {
  inline_keyboard: [
    [
      { text: "🇬🇧 English", callback_data: `${CB_LANG}:en` },
      { text: "🇮🇳 हिंदी", callback_data: `${CB_LANG}:hi` },
    ],
  ],
};

async function showStations(env, sql, chatId, lang, withWelcome) {
  const stations = await stationList(sql);
  const live = stations.filter((s) => s.live).length;
  const t = T[lang];
  const head = withWelcome
    ? `${t.welcome}\n\n<i>${t.disclaimer}</i>\n\n`
    : "";
  await send(
    env,
    chatId,
    head + fill(t.pickPlace, { live, total: stations.length }),
    stationKeyboard(stations, lang),
  );
}

async function onCommand(env, sql, chatId, text) {
  const command = text.trim().split(/[\s@]/)[0].toLowerCase();
  const known = await langOfChat(sql, chatId);
  const lang = known ?? "en";
  const t = T[lang];

  if (command === "/start" || command === "/stations") {
    // /start also clears a pause. send_alerts.py pauses a subscriber whose
    // chat answered 403, so this is the documented way back in.
    await sql`UPDATE subscribers SET is_paused = FALSE WHERE chat_id = ${chatId}`;

    // A first-time chat picks a language before it reads anything, because the
    // welcome itself has to be in some language and guessing wrong is the one
    // screen a person cannot get past. Someone who has already chosen skips
    // this and goes straight to the list — /language is where they change it.
    if (known === null) {
      await send(env, chatId, T.en.langPrompt, languageKeyboard);
      return;
    }
    await showStations(env, sql, chatId, lang, command === "/start");
    return;
  }

  if (command === "/language" || command === "/lang") {
    await send(env, chatId, t.langPrompt, languageKeyboard);
    return;
  }

  if (command === "/about") {
    await send(
      env,
      chatId,
      fill(t.about, {
        day: MAX_CHANGES_DAY,
        week: MAX_CHANGES_WEEK,
        disclaimer: `<i>${t.disclaimer}</i>`,
      }),
    );
    return;
  }

  if (command === "/pause") {
    const rows = await sql`
      UPDATE subscribers SET is_paused = TRUE WHERE chat_id = ${chatId}
      RETURNING chat_id
    `;
    await send(env, chatId, rows.length ? t.pausedYes : t.pausedNo);
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
    await send(env, chatId, rows.length ? t.stoppedYes : t.stoppedNo);
    return;
  }

  await send(env, chatId, t.help);
}

async function onCallback(env, sql, query) {
  const chatId = query.from.id;
  const parts = String(query.data || "").split(":");
  const kind = parts[0];

  if (kind === CB_LANG) {
    const lang = langOf(parts[1]);
    // An existing subscriber changes language in place and stays where they
    // are. A brand-new chat has no row to write to yet, so the choice travels
    // in callback_data until the profile tap creates one.
    const rows = await sql`
      UPDATE subscribers SET lang = ${lang} WHERE chat_id = ${chatId}
      RETURNING chat_id
    `;
    await answer(env, query.id, T[lang].langSetPopup);
    if (rows.length) {
      await send(env, chatId, T[lang].langSet);
      return;
    }
    await showStations(env, sql, chatId, lang, true);
    return;
  }

  if (kind === CB_DARK) {
    const lang = (await langOfChat(sql, chatId)) ?? "en";
    await answer(env, query.id, T[lang].darkPopup);
    return;
  }

  if (kind === CB_STATION) {
    const lang = langOf(parts[1]);
    const stationId = Number(parts[2]);
    const profiles = await sql`
      SELECT profile_id,
             CASE WHEN ${lang} = 'hi' THEN COALESCE(label_hi, label)
                  ELSE label END AS label,
             CASE WHEN ${lang} = 'hi' THEN COALESCE(description_hi, description)
                  ELSE description END AS description
      FROM profiles ORDER BY profile_id
    `;
    const stations = await sql`
      SELECT station_name FROM stations WHERE station_id = ${stationId}
    `;
    if (!stations.length) {
      await answer(env, query.id, T[lang].gone);
      return;
    }
    await answer(env, query.id);
    await send(
      env,
      chatId,
      `📍 <b>${esc(shortName(stations[0].station_name))}</b>\n\n` +
        `${T[lang].whoFor}\n\n` +
        profiles.map((p) => `• <b>${esc(p.label)}</b> — ${esc(p.description)}`).join("\n"),
      profileKeyboard(profiles, stationId, lang),
    );
    return;
  }

  if (kind === CB_PROFILE) {
    const lang = langOf(parts[1]);
    const stationId = Number(parts[2]);
    const profileId = parts[3];
    const t = T[lang];

    // Looked up before the insert, so a station retired between the keyboard
    // being drawn and the button being tapped answers a sentence rather than
    // raising a foreign-key error — which, because a non-2xx makes Telegram
    // redeliver, would retry forever.
    const stations = await sql`
      SELECT station_name FROM stations WHERE station_id = ${stationId}
    `;
    if (!stations.length) {
      await answer(env, query.id, t.gone);
      return;
    }
    // Read before the upsert, because after it there is no way left to tell a
    // first signup from a switch from a re-pick of the station they are
    // already on — and those three cases dispatch differently below.
    const before = await sql`
      SELECT station_id FROM subscribers WHERE chat_id = ${chatId}
    `;
    const isSwitch = before.length > 0 && before[0].station_id !== stationId;

    // The station always moves, on every path. Only the immediate send is
    // ever withheld.
    await sql`
      INSERT INTO subscribers (chat_id, station_id, profile_id, is_paused, lang)
      VALUES (${chatId}, ${stationId}, ${profileId}, FALSE, ${lang})
      ON CONFLICT (chat_id) DO UPDATE SET
        station_id = EXCLUDED.station_id,
        profile_id = EXCLUDED.profile_id,
        is_paused  = FALSE,
        lang       = EXCLUDED.lang
    `;

    const name = esc(shortName(stations[0].station_name));

    // Re-picking the station you are already on used to dispatch anyway. No
    // duplicate message came of it — send_alerts.py's already_sent() holds —
    // but the run started, woke Neon, found a sent_log row for
    // (chat_id, today, station_id) and sent nothing. A whole workflow for a
    // no-op, on every tap.
    if (before.length > 0 && !isSwitch) {
      await answer(env, query.id, t.samePopup);
      await send(env, chatId, fill(t.same, { name }));
      return;
    }

    if (isSwitch && !(await allowChange(sql, chatId))) {
      await answer(env, query.id, t.cappedPopup);
      await send(
        env,
        chatId,
        fill(t.capped, { name, day: MAX_CHANGES_DAY, week: MAX_CHANGES_WEEK }),
      );
      return;
    }

    // Fired before the confirmation, not after, and awaited. If the dispatch
    // fails this handler throws, Telegram redelivers the same tap, and the
    // upsert above runs again harmlessly — so the retry costs a repeated
    // spinner rather than a duplicate "Subscribed" message.
    //
    // On a redelivery the gate has already consumed one of the day's changes
    // and isSwitch is now false, so the retry takes the branch above and this
    // line is not reached twice.
    //
    // Today's message, not an extra one: send_alerts.py skips anyone already
    // holding a sent_log row for today, and sent_log's UNIQUE (chat_id,
    // send_date, station_id) is the backstop if two runs land together.
    await env.TRIGGER.fetch("https://trigger/send", { method: "POST" });

    await answer(env, query.id, t.subscribedPopup);
    await send(
      env,
      chatId,
      fill(t.subscribed, { name, day: MAX_CHANGES_DAY, week: MAX_CHANGES_WEEK }),
    );
    return;
  }

  if (kind === CB_FEEDBACK) {
    // The ownership check, and it is not optional. callback_data is echoed
    // back by the client, so without `AND chat_id = <the tapper>` anyone could
    // rate someone else's message by sending a forged id — and Gate 2 is
    // counted from this table.
    const rating = parts[2] === "-1" ? -1 : 1;
    const rows = await sql`
      INSERT INTO feedback (sent_log_id, chat_id, rating, station_id, pm25_value)
      SELECT id, chat_id, ${rating}, station_id, pm25_value
      FROM sent_log WHERE id = ${Number(parts[1])} AND chat_id = ${chatId}
      ON CONFLICT (sent_log_id, chat_id) DO UPDATE SET
        rating = EXCLUDED.rating, created_at = now()
      RETURNING id
    `;
    const lang = (await langOfChat(sql, chatId)) ?? "en";
    const t = T[lang];

    if (!rows.length) {
      await answer(env, query.id, t.notYours);
      return;
    }

    await answer(env, query.id, t.ratedPopup);
    // A pop-up vanishes in two seconds and leaves nothing behind, so a person
    // who looks away is left unsure the tap registered at all. The chat line
    // is the receipt.
    await send(env, chatId, rating > 0 ? t.thanksUp : t.thanksDown);

    // Take the buttons away once rated, so the message itself shows it is
    // done. Rating can still be changed by tapping the next day's message —
    // the ON CONFLICT above is what makes a re-rate an update rather than a
    // duplicate. Failure here is not worth losing the recorded rating over:
    // the row is already committed and the person already has their receipt.
    try {
      await tg(env, "editMessageReplyMarkup", {
        chat_id: chatId,
        message_id: query.message?.message_id,
        reply_markup: { inline_keyboard: [] },
      });
    } catch (e) {
      console.log(`could not clear feedback buttons: ${e.message}`);
    }
    return;
  }

  await answer(env, query.id);
}

// Records a station change and answers whether it was allowed, in one
// statement. Counting first and inserting second would let two fast taps both
// read "1 so far" and both pass; here the WHERE and the INSERT are the same
// statement, so the second tap counts the first.
//
// The purge rides along in a CTE rather than costing its own round trip.
// It cannot affect the counts: a CTE sees one snapshot, and rows older than
// seven days fall outside both windows anyway.
//
// tests/test_station_limit.py runs this same shape against Neon and reads the
// two constants above out of this file, so the numbers cannot drift apart.
async function allowChange(sql, chatId) {
  const rows = await sql`
    WITH purge AS (
      DELETE FROM station_changes WHERE changed_at < now() - interval '7 days'
    )
    INSERT INTO station_changes (chat_id)
    SELECT ${chatId}
    WHERE (SELECT count(*) FROM station_changes
           WHERE chat_id = ${chatId}
             AND changed_at > now() - interval '24 hours') < ${MAX_CHANGES_DAY}
      AND (SELECT count(*) FROM station_changes
           WHERE chat_id = ${chatId}
             AND changed_at > now() - interval '7 days') < ${MAX_CHANGES_WEEK}
    RETURNING chat_id
  `;
  return rows.length > 0;
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
