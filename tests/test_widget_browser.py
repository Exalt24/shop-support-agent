"""The widget in a real Chromium, on both demo sites, against the real chat server (uvicorn on a free port), the scripted model and
one fake Zoho per tenant. Nothing here is live.

Without Chromium these tests skip, so a laptop without a browser still runs the rest of the suite. In CI SHOP_SUPPORT_REQUIRE_BROWSER=1
turns that skip into a failure, so the browser half cannot go quiet. WIDGET_SCREENSHOT_DIR=docs keeps the screenshots that
docs/chat-widget.md shows; without it they go to a temporary folder.
"""
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from demo_widget import DemoStack

REQUIRE = os.environ.get("SHOP_SUPPORT_REQUIRE_BROWSER") == "1"
SHOTS = os.environ.get("WIDGET_SCREENSHOT_DIR")
QUOTE_A = "Hi, can I get a quote to replace our boiler? My name is Tom Reyes and my email is tom.reyes@example.com"
QUOTE_B = "Hello, can I get a quote for 40 towels for our hotel? My name is Ana Cruz and my email is ana.cruz@example.com"


def _unavailable(why: str):
    if REQUIRE:
        pytest.fail("SHOP_SUPPORT_REQUIRE_BROWSER is set but the browser half cannot run: " + why)
    pytest.skip(why)


@pytest.fixture(scope="module")
def stack():
    with DemoStack(extra_site_ports=1) as s:
        yield s


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _unavailable("playwright is not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no Chromium downloaded for this Playwright
            _unavailable("Chromium is not installed (python -m playwright install chromium): %s" % str(e).splitlines()[0])
        yield b
        b.close()


@pytest.fixture
def new_page(browser):
    contexts = []

    def make(width=1280, height=800, **options):
        ctx = browser.new_context(viewport={"width": width, "height": height}, **options)
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(8000)
        page.logs = []
        page.on("console", lambda m: page.logs.append(m.text))
        return page

    yield make
    for c in contexts:
        c.close()


def shot(page, tmp_path: Path, name: str) -> Path:
    folder = Path(SHOTS) if SHOTS else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    page.screenshot(path=str(path))
    return path


def open_chat(page, url):
    page.goto(url)
    page.locator("button.launcher").click()  # Playwright locators pierce the open shadow root
    page.get_by_role("dialog").wait_for()


def say(page, text):
    page.get_by_label(re.compile("Your message to")).fill(text)
    page.get_by_role("button", name="Send").click()


def bot_messages(page):
    return page.locator(".msg.bot")


def wait_for_reply(page, count):
    page.wait_for_function("n => document.querySelector('[data-support-chat]').shadowRoot.querySelectorAll('.msg.bot').length >= n", arg=count)


# ---- both sites: a routed enquiry, the consent button, a lead in the right CRM ------------------------------------------------

def test_site_a_routes_a_quote_and_the_yes_button_makes_a_lead_in_its_own_crm_only(stack, new_page, tmp_path):
    page = new_page()
    open_chat(page, stack.site_a)
    assert "Harbour Heating assistant" in bot_messages(page).first.inner_text()
    say(page, QUOTE_A)
    wait_for_reply(page, 2)
    assert "estimating team" in bot_messages(page).last.inner_text()
    assert page.get_by_role("button", name="Yes, share them").is_visible()
    assert page.evaluate("""() => { const l = document.querySelector('[data-support-chat]').shadowRoot.querySelector('.log');
        return l.scrollHeight - l.scrollTop - l.clientHeight <= 2; }""")          # the question itself is in view, not cut off
    assert stack.zoho["harbour"].leads == {}                       # nothing is sent before the yes
    shot(page, tmp_path, "chat-widget-site-a-1280-consent.png")
    page.get_by_role("button", name="Yes, share them").click()
    wait_for_reply(page, 3)
    assert not page.get_by_role("button", name="Yes, share them").is_visible()
    (lead,) = stack.zoho["harbour"].leads.values()
    assert (lead["First_Name"], lead["Last_Name"], lead["Email"]) == ("Tom", "Reyes", "tom.reyes@example.com")
    (task,) = stack.zoho["harbour"].tasks
    assert task["_lar_id"] == "LAR-HARBOUR-ESTIMATING"
    assert stack.zoho["loomhouse"].calls == []                      # the other site's CRM was never touched
    shot(page, tmp_path, "chat-widget-site-a-1280-done.png")


def test_site_b_sends_the_same_kind_of_enquiry_to_a_different_owner_queue(stack, new_page, tmp_path):
    before_a = len(stack.zoho["harbour"].leads)
    page = new_page()
    open_chat(page, stack.site_b)
    assert "Loomhouse" in bot_messages(page).first.inner_text()
    say(page, QUOTE_B)
    wait_for_reply(page, 2)
    assert "trade accounts team" in bot_messages(page).last.inner_text()
    shot(page, tmp_path, "chat-widget-site-b-1280-consent.png")
    page.get_by_role("button", name="Yes, share them").click()
    wait_for_reply(page, 3)
    (lead,) = stack.zoho["loomhouse"].leads.values()
    assert lead["Last_Name"] == "Cruz"
    (task,) = stack.zoho["loomhouse"].tasks
    assert task["_lar_id"] == "LAR-LOOMHOUSE-TRADE" and task["Subject"].startswith("Prepare a quote")
    assert "trade-accounts queue" in stack.zoho["loomhouse"].notes[0]["content"]
    assert len(stack.zoho["harbour"].leads) == before_a             # site A's CRM did not grow
    shot(page, tmp_path, "chat-widget-site-b-1280-done.png")


def test_the_no_button_sends_nothing_to_the_crm(stack, new_page):
    leads_before = len(stack.zoho["harbour"].leads)
    page = new_page()
    open_chat(page, stack.site_a)
    say(page, "Hello, can I get an estimate for a new bathroom? My name is Sam Lee and my email is sam.lee@example.com")
    wait_for_reply(page, 2)
    page.get_by_role("button", name="No thanks").click()
    wait_for_reply(page, 3)
    assert "won't pass your details" in bot_messages(page).last.inner_text()
    assert len(stack.zoho["harbour"].leads) == leads_before


# ---- hostile pages and hostile text ---------------------------------------------------------------------------------------------

def test_a_page_on_an_origin_no_tenant_lists_gets_no_chat_and_the_server_refuses_it(stack, new_page):
    stranger = stack.stranger_origins[0] + "/site_a.html"
    calls_before = {k: len(z.calls) for k, z in stack.zoho.items()}
    page = new_page()
    page.goto(stranger)
    time.sleep(1.0)  # the config request is refused by the browser's CORS check; there is no event to wait for
    assert page.locator("button.launcher").count() == 0
    assert any("chat is not available here" in m for m in page.logs)
    # the same page, asking the message route directly, cannot read an answer either
    blocked = page.evaluate("""async () => {
        try { await fetch(%r + '/api/chat/message', {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({site: 'pk_harbour_demo_01', message: 'hi'})}); return 'read'; } catch (e) { return 'blocked'; } }""" % stack.chat_origin)
    assert blocked == "blocked"
    r = httpx.post(stack.chat_origin + "/api/chat/message", json={"site": "pk_harbour_demo_01", "message": "hi"},
                   headers={"Origin": stack.stranger_origins[0]})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    assert {k: len(z.calls) for k, z in stack.zoho.items()} == calls_before  # no CRM was touched by any of it


def test_site_b_quoting_site_as_key_from_its_own_origin_is_refused(stack):
    r = httpx.post(stack.chat_origin + "/api/chat/message", json={"site": "pk_loomhouse_demo_02", "message": "hi"},
                   headers={"Origin": stack.origins["harbour-heating"]})
    assert r.status_code == 403


def test_an_html_injection_attempt_is_drawn_as_text_and_runs_nothing(stack, new_page, tmp_path):
    page = new_page()
    open_chat(page, stack.site_a)
    attack = '<img src=x onerror="window.__pwned=1"><b>bold</b><script>window.__pwned=2</script>'
    say(page, attack)
    wait_for_reply(page, 2)
    mine = page.locator(".msg.me").last
    assert attack in mine.inner_text()                               # shown exactly as typed
    assert page.evaluate("window.__pwned") is None
    assert page.evaluate("""() => { const r = document.querySelector('[data-support-chat]').shadowRoot;
        return r.querySelectorAll('.log img, .log b, .log script').length; }""") == 0
    shot(page, tmp_path, "chat-widget-injection-1280.png")


# ---- the page cannot restyle the widget, and the widget works for a keyboard ------------------------------------------------------

def test_each_site_gets_its_own_theme_and_the_host_pages_rough_css_cannot_reach_it(stack, new_page):
    colours = {}
    for name, url, pink_or_lime in (("a", stack.site_a, "rgb(255, 105, 180)"), ("b", stack.site_b, "rgb(0, 255, 0)")):
        page = new_page()
        page.goto(url)
        launcher = page.locator("button.launcher")
        launcher.wait_for()
        colours[name] = launcher.evaluate("e => getComputedStyle(e).backgroundColor")
        font = launcher.evaluate("e => getComputedStyle(e).fontFamily")
        assert "Comic Sans" not in font
        host_button = page.locator("#host-button").evaluate("e => getComputedStyle(e).backgroundColor")
        assert host_button == pink_or_lime                            # the page's own button really got its rough colour
    assert colours == {"a": "rgb(11, 93, 122)", "b": "rgb(138, 44, 74)"}  # #0b5d7a and #8a2c4a, whatever the page said


def test_keyboard_and_screen_reader_basics(stack, new_page):
    page = new_page()
    page.goto(stack.site_a)
    page.locator("button.launcher").wait_for()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")                                       # first the page's own button, then the widget launcher
    assert page.evaluate("document.activeElement.dataset.supportChat") == "pk_harbour_demo_01"
    page.keyboard.press("Enter")
    page.get_by_role("dialog").wait_for()
    assert page.get_by_label(re.compile("Your message to")).evaluate("e => e.getRootNode().activeElement === e")  # focus is in the box
    log = page.get_by_role("log")
    assert log.get_attribute("aria-live") == "polite" and log.get_attribute("aria-label") == "Conversation"
    assert page.locator("button.launcher").get_attribute("aria-expanded") == "true"
    page.keyboard.type(QUOTE_A)
    page.keyboard.press("Enter")                                     # Enter sends, Shift+Enter would add a line
    wait_for_reply(page, 2)
    assert page.get_by_role("button", name="Yes, share them").evaluate("e => e.getRootNode().activeElement === e")  # focus moved to the question
    page.keyboard.press("Escape")
    assert page.get_by_role("dialog").count() == 0 or not page.get_by_role("dialog").is_visible()
    assert page.locator("button.launcher").evaluate("e => e.getRootNode().activeElement === e")  # focus came back to where it started


def test_reduced_motion_is_respected(stack, new_page):
    page = new_page(reduced_motion="reduce")
    open_chat(page, stack.site_a)
    assert page.locator(".panel").evaluate("e => getComputedStyle(e).animationName") == "none"
    calm = new_page(reduced_motion="no-preference")
    open_chat(calm, stack.site_a)
    assert calm.locator(".panel").evaluate("e => getComputedStyle(e).animationName") == "rise"


@pytest.mark.parametrize("which", ["a", "b"])
def test_it_fits_at_360_pixels_with_no_sideways_scroll(stack, new_page, tmp_path, which):
    page = new_page(width=360, height=740)
    url = stack.site_a if which == "a" else stack.site_b
    open_chat(page, url)
    say(page, QUOTE_A if which == "a" else QUOTE_B)
    wait_for_reply(page, 2)
    box = page.locator(".panel").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 360 and box["y"] >= 0 and box["y"] + box["height"] <= 740
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    for name in ("Send", "Yes, share them", "No thanks", "Close chat"):
        b = page.get_by_role("button", name=name).bounding_box()
        assert b["height"] >= 44 and b["width"] >= 44, name         # touch targets
    shot(page, tmp_path, "chat-widget-site-%s-360-consent.png" % which)
