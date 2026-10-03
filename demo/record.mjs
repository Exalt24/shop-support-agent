/**
 * Record the support-console walkthrough against the LOCAL production build.
 *
 *   python -m console_api.seed                       (clean fake data; this run claims and resolves a case)
 *   next build && next start -p 3100   +   uvicorn console_api.main:app --port 8010
 *   python demo/word_times.py                         (when each word is spoken in the finished clips)
 *   node demo/record.mjs
 *
 * Rules honoured (each learned by breaking it, see the /demo-video skill):
 *  - MARKS ARE REAL WALL-CLOCK MOMENTS (Date.now() - REC0), taken AFTER a scene's content is visible. Audio and subtitles
 *    are placed at the marks, never at cumulative narration lengths.
 *  - EVERY action is tied to the WORD being spoken (`T(beat, "phrase")` reads work/word_times.json), not to a number of
 *    seconds. Fixed seconds went stale the moment the narration changed (dead air was cut and every cue moved).
 *  - EVERY visible action sits INSIDE its beat's narration window, because the assembler cuts silent stretches between beats.
 *  - A production build, not `next dev`: dev does not hydrate reliably under this Next version.
 *  - Fake data only. The app talks to the local Postgres and the development Shopify store, customers are @example.com,
 *    and this script refuses a non-local URL.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORK = path.join(HERE, "work");
const APP = process.env.CONSOLE_URL || "http://localhost:3100";
const W = 1600, H = 900;
const PAD = 0.55;

const { chromium } = await import("file:///C:/Users/Dax/AppData/Roaming/npm/node_modules/playwright/index.mjs");

const durs = {};
for (const b of JSON.parse(fs.readFileSync(path.join(WORK, "narration_final", "final.json"), "utf8")).beats) durs[b.name] = b.dur;
const WORDS = JSON.parse(fs.readFileSync(path.join(WORK, "word_times.json"), "utf8"));

/** seconds into the beat's narration at which `phrase` starts. Throws if the phrase is not spoken, so a changed script
 *  fails loudly here instead of firing an action at the wrong moment. */
function T(beat, phrase, plus = 0) {
  const want = phrase.toLowerCase().replace(/[^a-z0-9' ]/g, " ").split(/\s+/).filter(Boolean);
  const have = WORDS[beat].map((x) => x.w);
  for (let i = 0; i + want.length <= have.length; i++) {
    if (want.every((w, k) => have[i + k] === w)) return WORDS[beat][i].t + plus;
  }
  throw new Error(`phrase "${phrase}" is not spoken in ${beat}: ${have.join(" ")}`);
}

if (!/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(APP)) {
  console.error("REFUSING: CONSOLE_URL must be local, got " + APP);
  process.exit(1);
}

const VID = path.join(WORK, "video");
fs.rmSync(VID, { recursive: true, force: true });
fs.mkdirSync(VID, { recursive: true });

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  viewport: { width: W, height: H },
  recordVideo: { dir: VID, size: { width: W, height: H } },
  deviceScaleFactor: 1,
});
const page = await ctx.newPage();
const wait = (ms) => page.waitForTimeout(ms);
const REC0 = Date.now();
const marks = {};
const mark = (k) => { marks[k] = Date.now() - REC0; console.log(`mark ${k} @ ${(marks[k] / 1000).toFixed(2)}s`); };
const since = (k) => Date.now() - REC0 - marks[k];
/** wait until `sec` seconds after the beat's mark (no-op if already past) */
const at = async (k, sec) => { const w = sec * 1000 - since(k); if (w > 0) await wait(w); };
const holdEnd = (k) => at(k, durs[k] + PAD);

// ---- cursor + click ripple, injected into the page so they are captured in the recording ----
async function inject() {
  await page.evaluate(() => {
    if (!document.getElementById("__cs")) {
      const s = document.createElement("style");
      s.id = "__cs";
      s.textContent = `
        #__cur{position:fixed;width:24px;height:24px;border-radius:50%;background:rgba(79,70,229,.30);border:2px solid #4f46e5;z-index:100001;pointer-events:none;left:0;top:0;transition:transform .65s cubic-bezier(.22,1,.36,1);transform:translate(800px,450px)}
        @keyframes __rip{from{transform:scale(.3);opacity:.7}to{transform:scale(2.6);opacity:0}}`;
      document.head.appendChild(s);
    }
    if (!document.getElementById("__cur")) {
      const c = document.createElement("div");
      c.id = "__cur";
      document.body.appendChild(c);
    }
  });
}
async function box(loc) { try { return await loc.first().boundingBox(); } catch { return null; } }
async function moveTo(loc) {
  await inject();
  const b = await box(loc);
  if (!b) return;
  await page.evaluate((b) => {
    const c = document.getElementById("__cur");
    if (c) c.style.transform = `translate(${b.x + b.width / 2 - 12}px,${b.y + b.height / 2 - 12}px)`;
  }, b);
  await wait(700);
}
async function ripple(loc) {
  const b = await box(loc);
  if (!b) return;
  await page.evaluate((b) => {
    const d = document.createElement("div");
    d.style.cssText = `position:fixed;left:${b.x + b.width / 2 - 12}px;top:${b.y + b.height / 2 - 12}px;width:24px;height:24px;border-radius:50%;background:#4f46e5;z-index:100000;pointer-events:none;animation:__rip .6s ease-out forwards`;
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 600);
  }, b);
  await wait(180);
}
async function click(loc) { await moveTo(loc); await ripple(loc); await loc.first().click(); }
async function type(loc, text, delay = 45) { await click(loc); await loc.first().pressSequentially(text, { delay }); }

const nav = (name) => page.locator("nav[aria-label=Main]").getByRole("link", { name: new RegExp("^" + name) });
const visible = (loc, timeout = 30000) => loc.first().waitFor({ state: "visible", timeout });

// =========================================================== 01 intro + admin login
await page.goto(APP + "/login", { waitUntil: "domcontentloaded" });
await visible(page.locator("#email"));
await inject();
await wait(500);
mark("01_intro");
await at("01_intro", T("01_intro", "first the login"));
await type(page.locator("#email"), "admin@demo.test", 50);
await type(page.locator("#password"), "demo-admin-pass", 40);
await at("01_intro", T("01_intro", "what they can see"));
await click(page.getByRole("button", { name: "Sign in" }));
await visible(page.getByText("Conversations per day"));
await holdEnd("01_intro");

// =========================================================== 02 dashboard
await inject();
mark("02_dashboard");
await at("02_dashboard", T("02_dashboard", "conversations"));
await moveTo(page.getByText("Conversations", { exact: true }).first());
await at("02_dashboard", T("02_dashboard", "answered about"));
await moveTo(page.getByText("Answered by the agent").first());
await at("02_dashboard", T("02_dashboard", "the two on the right"));
await moveTo(page.getByText("Needs your team").first());
await holdEnd("02_dashboard");

// =========================================================== 03 inbox (a handled order question)
await click(nav("Inbox"));
await visible(page.locator('a[href*="inbox?c="]'));
await inject();
mark("03_inbox");
await at("03_inbox", 0.6);
await click(page.locator('a[href*="inbox?c="]').first());
await visible(page.getByText("Not shipped yet"));
await at("03_inbox", T("03_inbox", "then it said"));
await moveTo(page.getByText("Not shipped yet").first());
await at("03_inbox", T("03_inbox", "and under that"));
await moveTo(page.getByText("Looked up #1019 and the email matched").first());
await holdEnd("03_inbox");

// =========================================================== 04 privacy (a different email asks about that order)
await click(nav("Live chat"));
await visible(page.getByText("Talk to the agent as a customer"));
await inject();
mark("04_privacy");
await at("04_privacy", T("04_privacy", "asks about that same order"));
await click(page.getByRole("button", { name: "Someone else's order" }));
const privacyAnswer = page.waitForFunction(() => /find that order/i.test(document.body.innerText), null, { timeout: 90000 });
await holdEnd("04_privacy");
await privacyAnswer;

// =========================================================== 05 legal threat goes straight to a person
mark("05_legal");
await at("05_legal", 0.4);
await moveTo(page.locator("#customer"));
await page.locator("#customer").selectOption("ana.reyes@example.com");
await at("05_legal", 0.9);
await type(page.locator("#msg"), "If this isn't sorted today I'm speaking to my lawyer.", 38);
await at("05_legal", T("05_legal", "a rule in code"));
await click(page.getByRole("button", { name: "Send", exact: true }));
const legalAnswer = page.waitForFunction(() => /Handed to a person/i.test(document.body.innerText), null, { timeout: 90000 });
await holdEnd("05_legal");
await legalAnswer;

// =========================================================== 06 audit log
await click(nav("Audit log"));
await visible(page.getByText("Agent handed off"));
await inject();
mark("06_audit");
await at("06_audit", 1.0);
await moveTo(page.getByText("Agent handed off").first());
await at("06_audit", T("06_audit", "down to who looked"));
await click(page.getByRole("link", { name: "Order lookups" }));
await visible(page.getByText("Order looked up"));
await holdEnd("06_audit");

// =========================================================== 07 policy
await click(nav("Settings"));
await visible(page.locator("#policy"));
await inject();
mark("07_policy");
await at("07_policy", T("07_policy", "this is the policy"));
await moveTo(page.locator("#policy"));
await at("07_policy", T("07_policy", "change a line here"));
for (let i = 0; i < 6; i++) {
  await page.locator("#policy").evaluate((el) => { el.scrollTop += 45; });
  await wait(260);
}
await holdEnd("07_policy");

// =========================================================== 08 many stores, isolated
await click(page.locator('button[aria-controls="store-menu"]'));
await visible(page.getByRole("link", { name: /Northwind Gear/ }));
mark("08_stores");
await at("08_stores", T("08_stores", "switching to a second one"));
await click(page.getByRole("link", { name: /Northwind Gear/ }));
await visible(page.getByRole("heading", { name: "Northwind Gear" }));
await inject();
await at("08_stores", T("08_stores", "its own customers"));
await click(nav("Inbox"));
await visible(page.getByText("Sam Ortega"));
await holdEnd("08_stores");

// =========================================================== 09 the support person's login, claim the case
await click(page.locator('button[aria-label="Sign out"]'));
await visible(page.locator("#email"));
await inject();
mark("09_agent");
await at("09_agent", 0.2);
await type(page.locator("#email"), "agent@demo.test", 25);
await type(page.locator("#password"), "demo-agent-pass", 22);
await click(page.getByRole("button", { name: "Sign in" }));
await visible(page.getByText("Conversations per day"));
await inject();
await at("09_agent", T("09_agent", "no audit log"));
await moveTo(page.locator("nav[aria-label=Main]"));
await at("09_agent", T("09_agent", "the case the agent passed"));
await click(nav("Handoffs"));
await visible(page.locator("li", { hasText: "Ana Reyes" }));
await at("09_agent", T("09_agent", "claim it"));
const ana = page.locator("li", { hasText: "Ana Reyes" }).first();
await click(ana.getByRole("button", { name: "Claim" }));
await visible(page.getByText("You have this case"));
await holdEnd("09_agent");

// =========================================================== 09b open it, reply, resolve
mark("09b_resolve");
await at("09b_resolve", 0.3);
await click(ana.getByRole("link", { name: /Open case/ }));
await visible(page.locator("#reply"));
await inject();
await at("09b_resolve", T("09b_resolve", "everything the agent did"));
await moveTo(page.getByText("Handed to your team").first());
await at("09b_resolve", T("09b_resolve", "so nobody starts"));
await type(page.locator("#reply"), "Hi Ana, I'm sorry about the trouble. I've picked this up myself and I'm checking your order now.", 16);
await at("09b_resolve", T("09b_resolve", "and the customer has"));
await click(page.getByRole("button", { name: /Send reply and resolve/ }));
await visible(page.getByText(/^Resolved/));
await holdEnd("09b_resolve");

// =========================================================== 10 close
await click(nav("Dashboard"));
await visible(page.getByText("Conversations per day"));
await inject();
mark("10_close");
await at("10_close", 1.0);
await moveTo(page.getByText("Resolved by your team").first());
await holdEnd("10_close");

await wait(500);
const vp = await page.video().path();
await ctx.close();                                   // the .webm only flushes here
await browser.close();
fs.writeFileSync(path.join(WORK, "marks.json"), JSON.stringify({ video: vp, marks }, null, 1));
console.log("video:", vp);
