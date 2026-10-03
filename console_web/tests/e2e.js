/* End-to-end walkthrough of the console in a real browser: layout at 1280 and 390, both roles, the privacy case,
 * a claim-and-resolve, a handoff from live chat, tenant isolation on screen, and the audit trail.
 *
 *   python -m console_api.seed        (clean demo data: the run claims and resolves a case, so reseed between runs)
 *   CONSOLE_ALLOWED_ORIGINS=http://localhost:3100 python -m uvicorn console_api.main:app --port 8010
 *   cd console_web && npx next dev --port 3100
 *   node tests/e2e.js
 *
 * Needs Playwright (a global install is fine: set PLAYWRIGHT_PATH to its folder if require('playwright') fails).
 * Screenshots land in tests/shots/ (gitignored). Exit code 1 when any check fails.
 */
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const fs = require('fs');
const path = require('path');
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const BASE = process.env.CONSOLE_URL || 'http://localhost:3100';
const results = [];
const check = (name, ok, extra = '') => { results.push(`${ok ? 'PASS' : 'FAIL'}  ${name} ${extra}`); };

async function login(page, email, pw) {
  await page.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await page.fill('#email', email);
  await page.fill('#password', pw);
  await Promise.all([page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 20000 }), page.click('button[type=submit]')]);
  await page.waitForLoadState('networkidle').catch(() => {});
}
const overflow = (page) => page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));

(async () => {
  const browser = await chromium.launch({ headless: true });

  // ---------- 1. layout at both sizes, admin ----------
  for (const [label, vp] of [['desk', { width: 1280, height: 800 }], ['mob', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    await login(page, 'admin@demo.test', 'demo-admin-pass');
    const store = 'dac-dev-store';
    for (const p of ['', '/inbox', '/handoffs', '/chat', '/lookup', '/audit', '/settings']) {
      await page.goto(`${BASE}/${store}${p}`, { waitUntil: 'networkidle' });
      const o = await overflow(page);
      check(`${label} ${p || '/'} no horizontal overflow`, o.sw <= o.cw, `(scrollWidth ${o.sw} vs ${o.cw})`);
    }
    // open a conversation and verify the thread and the status chip sit inside the viewport
    await page.goto(`${BASE}/${store}/inbox`, { waitUntil: 'networkidle' });
    await page.locator('a[href*="inbox?c="]').first().click();
    await page.waitForLoadState('networkidle');
    const o = await overflow(page);
    check(`${label} open conversation no horizontal overflow`, o.sw <= o.cw, `(${o.sw} vs ${o.cw})`);
    const chip = page.locator('header >> text=/Answered by agent|Needs a person|Resolved/').first();
    const bb = await chip.boundingBox();
    check(`${label} status chip inside viewport`, !!bb && bb.x >= 0 && bb.x + bb.width <= vp.width, bb ? `x=${Math.round(bb.x)} w=${Math.round(bb.width)}` : 'missing');
    const bubble = page.locator('text=/Your order #1019/').first();
    const bb2 = await bubble.boundingBox();
    check(`${label} agent bubble inside viewport`, !!bb2 && bb2.x + bb2.width <= vp.width, bb2 ? `right=${Math.round(bb2.x + bb2.width)}` : 'missing');
    await page.screenshot({ path: `${OUT}/${label}-inbox-open.png` });
    // Details drawer (below 2xl the third pane becomes a drawer)
    const det = page.getByRole('link', { name: 'Show details' });
    if (await det.count()) {
      await det.click();
      await page.waitForURL(/[?&]d=1/);
      const aside = page.locator('aside[aria-label="Details"]');
      await aside.waitFor({ state: 'visible' });
      await page.waitForTimeout(400);
      const ab = await aside.boundingBox();
      check(`${label} details drawer inside viewport`, !!ab && ab.x >= 0 && ab.x + ab.width <= vp.width + 1, ab ? `x=${Math.round(ab.x)} w=${Math.round(ab.width)}` : 'missing');
      await page.screenshot({ path: `${OUT}/${label}-details.png` });
    }
    // Playwright's own caret-hiding during a screenshot can trip a hydration warning on the login form: not ours
    const real = errors.filter((e) => !/caret-color/.test(e));
    check(`${label} no console/page errors`, real.length === 0, JSON.stringify(real));
    await ctx.close();
  }

  // ---------- 2. agent role: claim, reply, resolve; no admin pages ----------
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    await login(page, 'agent@demo.test', 'demo-agent-pass');
    const nav = await page.locator('nav[aria-label=Main] a').allTextContents();
    check('agent nav has no Audit log or Settings', !nav.some((t) => /Audit|Settings/.test(t)), JSON.stringify(nav));
    for (const p of ['/audit', '/settings']) {
      await page.goto(`${BASE}/dac-dev-store${p}`, { waitUntil: 'networkidle' });
      check(`agent blocked from ${p}`, !new URL(page.url()).pathname.endsWith(p), `-> ${new URL(page.url()).pathname}`);
    }
    const nw = await page.goto(`${BASE}/northwind-gear`, { waitUntil: 'networkidle' });
    const body = await page.locator('body').innerText();
    check('agent cannot open a store they are not in (404, not 403)', nw.status() === 404 || /could not find/i.test(body), `status ${nw.status()}`);
    // claim and resolve the legal-threat case
    await page.goto(`${BASE}/dac-dev-store/handoffs`, { waitUntil: 'networkidle' });
    const card = page.locator('li', { hasText: 'Tom Novak' }).first();
    await card.getByRole('button', { name: 'Claim' }).click();
    await page.waitForSelector('text=You have this case', { timeout: 15000 });
    check('claim shows ownership', true);
    await card.getByRole('link', { name: /Open case/ }).click();
    await page.waitForLoadState('networkidle');
    await page.fill('#reply', 'Hi Tom, I am a member of the team and I am looking into order 1013 now. I will email you from here.');
    await page.fill('#note', 'Legal wording, kept to facts only');
    const sendBox = await page.getByRole('button', { name: /Send reply and resolve/ }).boundingBox();
    const detBox = await page.getByRole('link', { name: 'Show details' }).boundingBox();
    const hit = sendBox && detBox && !(detBox.x + detBox.width < sendBox.x || detBox.x > sendBox.x + sendBox.width || detBox.y + detBox.height < sendBox.y || detBox.y > sendBox.y + sendBox.height);
    check('Details control does not overlap the resolve button', !hit, JSON.stringify({ sendBox, detBox }));
    await page.screenshot({ path: `${OUT}/desk-reply.png` });
    await page.getByRole('button', { name: /Send reply and resolve/ }).click();
    await page.waitForSelector('text=/Resolved/', { timeout: 15000 });
    check('reply and resolve completes', true);
    await page.screenshot({ path: `${OUT}/desk-resolved.png` });

    // ---------- 3. live chat: the privacy case, then a handoff ----------
    await page.goto(`${BASE}/dac-dev-store/chat`, { waitUntil: 'networkidle' });
    await page.getByRole('button', { name: "Someone else's order" }).click();
    await page.waitForFunction(() => /Answered by the agent|Handed to a person/.test(document.body.innerText), null, { timeout: 90000 });
    const chatText = await page.locator('[aria-label=Conversation]').innerText();
    check('wrong-email order gets no order details', !/shipped|tracking number is|USB Audio|250\.00/i.test(chatText), JSON.stringify(chatText.slice(0, 300)));
    await page.screenshot({ path: `${OUT}/desk-chat-privacy.png` });
    await page.getByRole('button', { name: 'Legal threat' }).click();
    await page.waitForFunction(() => /Handed to a person/.test(document.body.innerText), null, { timeout: 90000 });
    check('legal threat is handed to a person', true);
    await page.screenshot({ path: `${OUT}/desk-chat-handoff.png` });
    await page.goto(`${BASE}/dac-dev-store/handoffs`, { waitUntil: 'networkidle' });
    check('the new handoff shows in the queue', (await page.locator('li', { hasText: 'Someone Else' }).count()) + (await page.locator('li', { hasText: 'Legal threat' }).count()) > 0);

    // ---------- 4. lookup ----------
    await page.goto(`${BASE}/dac-dev-store/lookup`, { waitUntil: 'networkidle' });
    await page.fill('#q', '1019');
    await page.getByRole('button', { name: 'Look up' }).click();
    await page.waitForSelector('text=#1019', { timeout: 30000 });
    check('order lookup shows the order', (await page.locator('text=USB Audio Interface').count()) > 0);
    await page.screenshot({ path: `${OUT}/desk-lookup.png` });
    await ctx.close();
  }

  // ---------- 5. admin sees both stores; isolation on screen; the audit trail ----------
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    await login(page, 'admin@demo.test', 'demo-admin-pass');
    await page.goto(`${BASE}/northwind-gear/inbox`, { waitUntil: 'networkidle' });
    const body = await page.locator('main').innerText();
    check('second store shows only its own conversations', /sam\.ortega|Sam Ortega/i.test(body) && !/Miguel Santos/.test(body));
    await page.screenshot({ path: `${OUT}/desk-northwind.png` });
    await page.goto(`${BASE}/dac-dev-store/audit`, { waitUntil: 'networkidle' });
    await page.screenshot({ path: `${OUT}/desk-audit-after.png` });
    const auditText = await page.locator('main').innerText();
    check('audit log recorded the claim, the resolve and the lookup', /Case claimed/.test(auditText) && /Case resolved/.test(auditText) && /Order looked up/.test(auditText));
    await ctx.close();
  }

  await browser.close();
  console.log(results.join('\n'));
  const failed = results.filter((r) => r.startsWith('FAIL')).length;
  console.log(`\n${results.length - failed} pass, ${failed} fail`);
  if (failed) process.exit(1);
})().catch((e) => { console.error(results.join('\n')); console.error(e); process.exit(1); });
