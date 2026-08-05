// Jobbie's interactive half — a Cloudflare Worker on Telegram's webhook.
//
// Instant answers happen here (/help, /profile). Anything heavy — JSearch
// queries, resume PDF parsing — is dispatched to the GitHub Actions workflow,
// which replies to Telegram itself a minute later. The split is a CPU budget:
// a free Worker gets ~10ms per request, Python in Actions gets minutes.
//
// SINGLE USER BY DESIGN: updates from any other chat are silently dropped.

const HELP = `<b>Jobbie commands</b>

/jobs — hunt for new jobs right now (~1 min)
/excel — every job sent so far, as a file that opens in Excel
/profile — what I'm currently searching for you
📄 Send me a resume PDF — I retune the searches to it
/help — this list

<i>Fresh jobs arrive automatically at 9:00 and 18:00 IST — each with an apply link, recruiter/engineer finders, and a copyable referral message.</i>`;

const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

const say = (env, text) =>
  tg(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });

async function dispatch(env, inputs = {}) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return "⚠️ <b>This button needs a GitHub token.</b>\n\n"
      + "1. github.com/settings/personal-access-tokens/new\n"
      + "2. Repository access → Only select repositories → Jobbie\n"
      + "3. Permissions → Actions → Read and write\n"
      + "4. Then: <code>cd jobbie/worker && npx wrangler secret put GITHUB_TOKEN</code>";
  }
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/jobs.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "jobbie-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  if (res.status === 204) return null;
  return `GitHub refused (${res.status}): ${esc((await res.text()).slice(0, 200))}`;
}

async function cmdExcel(env) {
  const res = await fetch(
    // raw.githubusercontent caches ~5 min; the timestamp param busts it
    `https://raw.githubusercontent.com/${env.GITHUB_REPO}/main/data/jobs.csv?${Date.now()}`,
    { headers: { "User-Agent": "jobbie-worker" }, cf: { cacheTtl: 0 } },
  );
  if (!res.ok) return "No jobs logged yet — the file appears after the next run that sends a job.";
  const form = new FormData();
  form.append("chat_id", env.TELEGRAM_CHAT_ID);
  form.append("caption", "📊 All jobs so far — opens in Excel."
    + (env.SHEET_URL ? `\n\n📈 Live tracker: ${env.SHEET_URL}` : ""));
  form.append("document", await res.blob(), "jobs.csv");
  const sent = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`,
    { method: "POST", body: form },
  );
  return sent.ok ? null : "Couldn't send the file, try again in a bit.";
}

async function cmdProfile(env) {
  const res = await fetch(
    `https://raw.githubusercontent.com/${env.GITHUB_REPO}/main/data/profile.json?${Date.now()}`,
    { headers: { "User-Agent": "jobbie-worker" }, cf: { cacheTtl: 0 } },
  );
  if (!res.ok) return "Couldn't read the profile right now.";
  const p = await res.json();
  return `<b>🔎 Search profile</b> (updated ${esc(p.updated)})\n\n`
    + p.queries.map((q) => "• " + esc(q)).join("\n")
    + `\n\nMax experience asked: ${Math.round((p.max_months || 36) / 12)} yrs`
    + `\n\nReferral pitch:\n<i>${esc(p.pitch)}</i>`
    + `\n\n<i>Send a resume PDF to retune, or edit data/profile.json in the repo.</i>`;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("jobbie is alive");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json().catch(() => ({}));
    const msg = update.message || update.edited_message;
    if (!msg || String(msg.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("ok"); // always 200 so Telegram stops retrying
    }

    let reply;
    try {
      const doc = msg.document || {};
      if ((doc.file_name || "").toLowerCase().endsWith(".pdf")) {
        reply = (await dispatch(env, { file_id: doc.file_id }))
          || "📄 Got your resume — retuning the searches and running a fresh hunt. Results in ~1 minute.";
      } else {
        const cmd = (msg.text || "").trim().split(/\s+/)[0].split("@")[0].toLowerCase();
        switch (cmd) {
          case "/start":
          case "/help":
            reply = HELP; break;
          case "/jobs":
            reply = (await dispatch(env))
              || "🔍 Hunting — new jobs land here in ~1 minute.";
            break;
          case "/excel":
          case "/csv":
            reply = await cmdExcel(env); break;
          case "/profile":
            reply = await cmdProfile(env); break;
          default:
            reply = cmd.startsWith("/") ? `Don't know <code>${esc(cmd)}</code>. Try /help.` : null;
        }
      }
    } catch (e) {
      reply = `Something broke handling that: ${esc(e.message)}`;
    }

    if (reply) await say(env, reply);
    return new Response("ok");
  },
};
