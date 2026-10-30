/*
 * Embeddable support chat. One script tag, no dependencies:
 *
 *   <script src="https://chat.example.com/widget/widget.js" data-site="pk_your_public_key" async></script>
 *
 * It talks to serve.py (/api/chat/config and /api/chat/message) on the origin the script was loaded from, or on data-api.
 * Rules this file keeps:
 *   - Shadow DOM: the host page's CSS cannot reach the widget and the widget's CSS cannot reach the page.
 *     Inherited properties still cross a shadow boundary (a page rule like `* { text-transform: uppercase }` reaches the host element
 *     and flows in), so the wrapper inside resets every property with `all: initial` before it sets its own.
 *   - Text only: every string from the visitor or the server goes in as a text node. No markup is ever parsed.
 *   - Never break the host: setup runs inside try/catch and a failure leaves the page exactly as it was.
 *   - No cookies, no storage: requests carry no credentials and the conversation id lives in memory until the page closes.
 *   - Keyboard and screen reader: labelled controls, a log region that announces replies, focus goes where the next action is,
 *     Escape closes, and animation only when the visitor has not asked for less motion.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var YES_TEXT = "Yes, please go ahead.";
  var NO_TEXT = "No, thanks.";
  var HEX = /^#[0-9a-fA-F]{6}$/;

  function start() {
    if (!script || !script.src) return;
    var site = script.getAttribute("data-site");
    if (!site) { warn("data-site is missing"); return; }
    var registry = (window.__shopSupportChat = window.__shopSupportChat || {});
    if (registry[site]) return; // the same site key twice on one page is one widget
    var apiBase = apiOrigin(script);
    if (!apiBase) { warn("data-api is not an http(s) origin"); return; }
    registry[site] = true;

    request(apiBase, "/api/chat/config?site=" + encodeURIComponent(site), null).then(function (r) {
      if (r.status !== 200 || !r.body || typeof r.body.name !== "string") { warn("chat is not available here"); return; }
      if (document.body) mount(apiBase, site, r.body);
      else document.addEventListener("DOMContentLoaded", function () { mount(apiBase, site, r.body); });
    }, function () { warn("chat is not available here"); });
  }

  function warn(text) {
    if (window.console && console.warn) console.warn("[support chat] " + text);
  }

  function apiOrigin(el) {
    var chosen = el.getAttribute("data-api");
    try {
      var u = new URL(chosen || el.src, document.baseURI);
      return /^https?:$/.test(u.protocol) ? u.origin : null;
    } catch (e) { return null; }
  }

  function request(base, path, body) {
    var init = { mode: "cors", credentials: "omit", cache: "no-store" };
    if (body) {
      init.method = "POST";
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    return fetch(base + path, init).then(function (res) {
      return res.json().then(function (j) { return { status: res.status, body: j }; },
        function () { return { status: res.status, body: {} }; });
    });
  }

  // COPY-OK: the minus signs in the calc() expressions below are CSS, not prose
  var CSS = [
    ":host { all: initial; }",
    "[hidden] { display: none !important; }",
    "* { box-sizing: border-box; }",
    ".root { all: initial; display: block; color-scheme: light; font: 16px/1.4 system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1c2024; }",
    ".launcher { position: fixed; right: 16px; bottom: 16px; width: 56px; height: 56px; border-radius: 50%; border: 0; padding: 0;",
    "  background: var(--primary); color: var(--on-primary); cursor: pointer; display: grid; place-items: center; z-index: 2147483000;",
    "  box-shadow: 0 2px 10px rgba(0,0,0,.3); }",
    ".launcher svg { width: 26px; height: 26px; fill: currentColor; }",
    ".panel { position: fixed; right: 16px; bottom: 84px; width: 380px; height: 560px;",
    "  max-width: calc(100vw - 32px); max-height: calc(100vh - 104px); max-height: calc(100dvh - 104px);", // COPY-OK: CSS calc
    "  display: flex; flex-direction: column; background: #fff;",
    "  border: 1px solid #c4c9cf; border-radius: 14px; overflow: hidden; z-index: 2147483000; box-shadow: 0 8px 30px rgba(0,0,0,.28); }",
    ".head { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: var(--primary); color: var(--on-primary); }",
    ".avatar { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; font-size: 14px;",
    "  background: var(--on-primary); color: var(--primary); flex: none; }",
    ".title { flex: 1; min-width: 0; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }",
    ".close { width: 44px; height: 44px; border: 0; background: transparent; color: inherit; font-size: 26px; line-height: 1; cursor: pointer; border-radius: 8px; }",
    ".log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; background: #fff; }",
    ".msg { max-width: 86%; padding: 9px 12px; border-radius: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }",
    ".bot { align-self: flex-start; background: #eef0f2; color: #1c2024; border-bottom-left-radius: 4px; }",
    ".me { align-self: flex-end; background: var(--primary); color: var(--on-primary); border-bottom-right-radius: 4px; }",
    ".note { align-self: center; background: #fff4d6; color: #4a3600; border: 1px solid #e3c16a; font-size: 14px; }",
    ".sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }",
    ".status { min-height: 22px; padding: 0 14px 4px; font-size: 14px; color: #4b5563; }",
    ".consent { display: flex; gap: 8px; padding: 6px 12px 10px; }",
    ".consent button { flex: 1; min-height: 44px; border-radius: 10px; font: inherit; font-weight: 600; cursor: pointer; }",
    ".yes { background: var(--primary); color: var(--on-primary); border: 2px solid var(--primary); }",
    ".no { background: #fff; color: #1c2024; border: 2px solid #5b6470; }",
    ".composer { display: flex; gap: 8px; padding: 10px 12px; border-top: 1px solid #d5d9de; align-items: flex-end; }",
    ".composer textarea { flex: 1; min-height: 44px; max-height: 120px; resize: none; padding: 10px; font: inherit; color: #1c2024;",
    "  background: #fff; border: 2px solid #5b6470; border-radius: 10px; }",
    ".send { min-width: 64px; min-height: 44px; padding: 0 14px; border: 0; border-radius: 10px; font: inherit; font-weight: 600;",
    "  background: var(--primary); color: var(--on-primary); cursor: pointer; }",
    ".send[aria-disabled='true'] { opacity: .6; cursor: default; }",
    ".foot { margin: 0; padding: 0 12px 10px; font-size: 12px; color: #4b5563; }",
    "button:focus-visible, textarea:focus-visible, .log:focus-visible { outline: 3px solid #1c2024; outline-offset: 2px; box-shadow: 0 0 0 5px #fff; }",
    ".head .close:focus-visible { outline-color: var(--on-primary); box-shadow: none; }",
    ".dots::after { content: '...'; }",
    "@media (max-width: 480px) {",
    "  .panel { left: 8px; right: 8px; width: auto; max-width: none; bottom: 76px;",
    "    max-height: calc(100vh - 92px); max-height: calc(100dvh - 92px); }", // COPY-OK: CSS calc
    "  .launcher { right: 12px; bottom: 12px; } }",
    "@media (prefers-reduced-motion: no-preference) {",
    "  .panel { animation: rise .18s ease-out; }",
    "  .dots::after { animation: blink 1.2s steps(4, end) infinite; }",
    "  @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }",
    "  @keyframes blink { 0% { content: ''; } 34% { content: '.'; } 67% { content: '..'; } 100% { content: '...'; } } }",
    "@media (forced-colors: active) { .launcher, .send, .yes, .no, .msg { border: 1px solid CanvasText; } }"
  ].join("\n");

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text != null) n.textContent = text;
    return n;
  }

  function bubbleIcon() {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var p = document.createElementNS(ns, "path");
    p.setAttribute("d", "M4 3h16a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z");
    svg.appendChild(p);
    return svg;
  }

  function mount(apiBase, site, cfg) {
    var host = el("div", { "data-support-chat": site });
    var root = host.attachShadow({ mode: "open" });
    var primary = HEX.test(cfg.primary) ? cfg.primary : "#1f4e79";
    var onPrimary = HEX.test(cfg.on_primary) ? cfg.on_primary : "#ffffff";
    host.style.setProperty("--primary", primary);
    host.style.setProperty("--on-primary", onPrimary);
    try {
      var sheet = new CSSStyleSheet(); // adopted sheets are not blocked by a host page's style-src policy
      sheet.replaceSync(CSS);
      root.adoptedStyleSheets = [sheet];
    } catch (e) {
      root.appendChild(el("style", null, CSS));
    }

    var name = String(cfg.name).slice(0, 40);
    var wrap = el("div", { "class": "root" });

    var launcher = el("button", { type: "button", "class": "launcher", "aria-label": "Chat with " + name,
      "aria-expanded": "false", "aria-controls": "panel" });
    launcher.appendChild(bubbleIcon());

    var panel = el("section", { id: "panel", "class": "panel", role: "dialog", "aria-label": "Chat with " + name });
    panel.hidden = true;
    var head = el("div", { "class": "head" });
    head.appendChild(el("span", { "class": "avatar", "aria-hidden": "true" }, String(cfg.initials || "").slice(0, 3)));
    head.appendChild(el("span", { "class": "title" }, name));
    var close = el("button", { type: "button", "class": "close", "aria-label": "Close chat" }, "×");
    head.appendChild(close);

    var log = el("div", { "class": "log", role: "log", "aria-live": "polite", "aria-relevant": "additions",
      "aria-label": "Conversation", tabindex: "0" });
    var status = el("div", { "class": "status", role: "status" });
    var consent = el("div", { "class": "consent", role: "group", "aria-label": "Share your details with the team?" });
    consent.hidden = true;
    var yes = el("button", { type: "button", "class": "yes" }, "Yes, share them");
    var no = el("button", { type: "button", "class": "no" }, "No thanks");
    consent.appendChild(yes);
    consent.appendChild(no);

    var form = el("form", { "class": "composer", autocomplete: "off" });
    var label = el("label", { "class": "sr", "for": "msg" }, "Your message to " + name);
    var input = el("textarea", { id: "msg", rows: "1", maxlength: "1000", enterkeyhint: "send" });
    var send = el("button", { type: "submit", "class": "send", "aria-disabled": "false" }, "Send");
    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(send);
    var foot = el("p", { "class": "foot" }, "Automated assistant. Please do not type card numbers or passwords.");

    [head, log, status, consent, form, foot].forEach(function (n) { panel.appendChild(n); });
    wrap.appendChild(panel);
    wrap.appendChild(launcher);
    root.appendChild(wrap);
    document.body.appendChild(host);

    var token = null;
    var busy = false;
    var greeted = false;

    function add(kind, text) {
      var m = el("div", { "class": "msg " + kind });
      var who = kind === "me" ? "You said: " : kind === "bot" ? name + " said: " : "Notice: ";
      m.appendChild(el("span", { "class": "sr" }, who));
      m.appendChild(document.createTextNode(String(text))); // text node: markup in a message is shown, never run
      log.appendChild(m);
      log.scrollTop = log.scrollHeight;
    }

    function showConsent() {
      consent.hidden = false;           // the buttons take room from the log, so the last message is scrolled back into view
      log.scrollTop = log.scrollHeight;
      yes.focus();
    }

    function setBusy(on) {
      busy = on;
      send.setAttribute("aria-disabled", on ? "true" : "false");
      log.setAttribute("aria-busy", on ? "true" : "false");
      status.textContent = "";
      if (on) {
        status.appendChild(document.createTextNode(name + " is typing"));
        status.appendChild(el("span", { "class": "dots", "aria-hidden": "true" }));
      }
    }

    function open() {
      panel.hidden = false;
      launcher.setAttribute("aria-expanded", "true");
      if (!greeted) { greeted = true; add("bot", cfg.greeting); }
      if (consent.hidden) input.focus(); else yes.focus();
    }

    function shut() {
      panel.hidden = true;
      launcher.setAttribute("aria-expanded", "false");
      launcher.focus();
    }

    function submit(text, restore, fromButton) {
      if (busy || !text.trim()) return;
      add("me", text);
      consent.hidden = true;
      setBusy(true);
      var body = { site: site, message: text };
      if (token) body.conversation_id = token;
      request(apiBase, "/api/chat/message", body).then(function (r) {
        setBusy(false);
        var b = r.body || {};
        if (r.status === 200 && typeof b.reply === "string") {
          token = typeof b.conversation_id === "string" ? b.conversation_id : token;
          add("bot", b.reply);
          if (b.awaiting_consent === true) showConsent(); else input.focus();
          return;
        }
        fail(r.status, b, text, restore, fromButton);
      }, function () {
        setBusy(false);
        fail(0, {}, text, restore, fromButton);
      });
    }

    function fail(statusCode, b, text, restore, fromButton) {
      if (statusCode === 429) {
        add("note", "You are sending messages quickly. Please wait " + (parseInt(b.retry_after, 10) || 30) + " seconds and try again.");
      } else if (statusCode === 400 && b.error === "bad_conversation") {
        token = null;
        add("note", "That chat timed out, so a new one will start with your next message.");
      } else if (statusCode === 413 || (statusCode === 400 && b.error === "bad_request")) {
        add("note", "That message could not be sent. Please try a shorter one.");
      } else {
        add("note", "The chat is not available right now. Please try again in a moment.");
      }
      if (restore) input.value = text;
      if (fromButton) showConsent(); else input.focus();  // the question is still open
    }

    function grow() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight + 4, 120) + "px";
    }

    launcher.addEventListener("click", function () { if (panel.hidden) open(); else shut(); });
    close.addEventListener("click", shut);
    panel.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.stopPropagation(); shut(); } });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value;
      if (busy || !text.trim()) return;
      input.value = "";
      grow();
      submit(text, true);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    input.addEventListener("input", grow);
    // The buttons send the same plain words a visitor could type, so the server's own consent check is what counts them.
    yes.addEventListener("click", function () { submit(YES_TEXT, false, true); });
    no.addEventListener("click", function () { submit(NO_TEXT, false, true); });
  }

  try {
    start();
  } catch (e) {
    warn("could not start");
  }
})();
