"""Enquiry routing and lead capture, shown on five scripted enquiries. EVERYTHING HERE IS A FAKE.

    python demo_enquiry.py

The model is a script (it answers each triage prompt with a fixed JSON, the way a real one might), the shop is a stub and Zoho
CRM is the in-process fake in agent/zoho_fake.py. No Zoho account, no Shopify store, no model API and no network is touched;
the only thing that ever left this machine for this feature was a fetch of Zoho's public documentation (docs/vendor-contract/).
What the run shows is what the CODE does with those proposals: where each enquiry is routed, who is asked for consent, and the
exact calls that WOULD go to Zoho. It writes demo_enquiry_transcript.md.

The clock is fixed to Friday 2026-10-30 and the account region is `au`, so the transcript is reproducible and shows the
.com.au domains an Australian account uses.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from agent.audit import AuditLog
from agent.core import TRIAGE_SYSTEM, SupportAgent
from agent.enquiry import EnquiryDesk
from agent.leads import LeadCapture
from agent.llm import LLMError
from agent.shopify import OrderFacts
from agent.zoho_crm import ZohoCrmClient, ZohoSettings
from agent.zoho_fake import FakeZoho

HERE = Path(__file__).resolve().parent
TODAY = date(2026, 10, 30)
CALM = {"angry": False, "legal_threat": False, "chargeback": False, "wants_human": False}


def triage(intent: str, **lead) -> dict:
    """What a scripted model proposes for one message: an intent from the list, and lead values copied from the message."""
    return {"intent": "other", "order_number": None, "product_query": None, "enquiry_intent": intent, "lead": lead, **CALM}


class ScriptedModel:
    def __init__(self, triages: list[dict]):
        self.triages = list(triages)

    def complete(self, system, user, *, max_tokens=1800):
        if system != TRIAGE_SYSTEM or not self.triages:
            raise LLMError("the demo model only triages, and has nothing left to say")
        return json.dumps(self.triages.pop(0))


class NoShop:
    def find_order(self, order_name, email) -> OrderFacts | None:
        return None

    def search_products(self, text, limit=3):
        return []


# (title, conversation id, visitor email, [(visitor message, what the scripted model proposes for it)])
SCENARIOS = [
    ("1. A sales enquiry, and the visitor says yes", "web-1", "priya.nair@example.com", [
        ("Hi, we run three cafes and want to talk about wholesale pricing. I'm Priya Nair, priya.nair@example.com",
         triage("sales_enquiry", name="Priya Nair", email="priya.nair@example.com")),
        ("Yes please, go ahead.", triage("general"))]),
    ("2. A quote request, and the visitor says no", "web-2", "miguel.santos@example.com", [
        ("Can I get a quote for fitting out 20 desks? Miguel Santos, call me on 0917 123 4567",
         triage("quote_request", name="Miguel Santos", phone="0917 123 4567")),
        ("Not right now, thanks.", triage("general"))]),
    ("3. A service request, with consent", "web-3", "sam.lee@example.com", [
        ("The compressor at our Cebu site is down. Can someone come out? Sam Lee, sam.lee@example.com, we are Lee Cold Storage",
         triage("service_request", name="Sam Lee", email="sam.lee@example.com", company="Lee Cold Storage")),
        ("Yes, that's fine.", triage("general"))]),
    ("4. An angry message with a legal threat", "web-4", "dana.cruz@example.com", [
        ("This is the third time nobody has answered my quote. I'm calling my lawyer today.", triage("quote_request"))]),
    ("5. The same email as scenario 1, a second enquiry the same day", "web-5", "priya.nair@example.com", [
        ("Hi again, Priya Nair here. Can you also send details on delivery to Dumaguete?",
         triage("sales_enquiry", name="Priya Nair", email="priya.nair@example.com")),
        ("Yes please.", triage("general"))]),
]


def describe(call: tuple[str, str, object]) -> str:
    method, url, body = call
    u = urlparse(url)
    if u.path == "/oauth/v2/token":
        return "%s %s%s  (token refresh, credentials not shown)" % (method, u.netloc, u.path)
    return "%s %s%s\n      %s" % (method, u.netloc, u.path, json.dumps(body, ensure_ascii=False))


def main() -> int:
    zoho = FakeZoho("au")
    crm = ZohoCrmClient(ZohoSettings("demo-client-id", "demo-client-secret", "demo-refresh-token", "au"), zoho, sleep=lambda s: None)
    desk = EnquiryDesk(LeadCapture(crm, tenant="demo-shop", today=lambda: TODAY, assignment_rules={}), tenant="demo-shop")
    audit_path = Path(tempfile.mkdtemp(prefix="enquiry-demo-")) / "audit.jsonl"
    audit = AuditLog(audit_path)

    md = ["# Enquiry routing demo transcript", "",
          "**This is a run against FAKES.** A scripted model, a stub shop and an in-process fake Zoho CRM (`agent/zoho_fake.py`, "
          "region `au`). No live Zoho account, no Shopify store, no model API and no network. The clock is fixed to Friday 2026-10-30. "
          "The lines under \"Would be sent to Zoho\" are the exact requests the client built; they went to the fake.", ""]
    print("DEMO WITH FAKES ONLY: scripted model, stub shop, fake Zoho (region au). No live Zoho, no network.")
    for title, conv, email, turns in SCENARIOS:
        model = ScriptedModel([t for _, t in turns])
        agent = SupportAgent(model, NoShop(), [], audit, None, desk)
        print("\n=== %s ===" % title)
        md += ["## " + title, "", "Conversation `%s`, visitor email `%s`." % (conv, email), ""]
        for message, _ in turns:
            seen = len(zoho.calls)
            res = agent.handle(message, email, conv)
            sent = [describe(c) for c in zoho.calls[seen:]]
            e = res.enquiry
            routed = ("intent=%s queue=%s follow-up=%s | consent=%s captured=%s lead=%s vendor=%s" % (
                e.intent, e.queue, e.follow_up, "yes" if e.consent else "no", "yes" if e.captured else "no", e.lead_status, e.vendor_code)
                      ) if e else "(escalated before routing: the floor ran first and the model was never called)"
            print("  visitor : %s\n  agent   : %s [%s%s]\n  routed  : %s" % (
                message, res.reply, res.action, ", " + res.escalation_reason if res.escalation_reason else "", routed))
            for line in sent:
                print("  zoho    : " + line)
            if not sent:
                print("  zoho    : (nothing sent)")
            md += ["- **Visitor:** %s" % message, "- **Agent (%s%s):** %s" % (
                res.action, ", " + res.escalation_reason if res.escalation_reason else "", res.reply), "- **Routed:** %s" % routed,
                   "- **Would be sent to Zoho:** " + ("" if sent else "nothing")]
            md += ["  - `%s`" % line.replace("\n      ", " ") for line in sent]
            md.append("")
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    md += ["## What the audit log kept", "",
           "One line per turn; this is the `enquiry` part of each line that has one. Counts and kinds only: no name, no email, no phone, "
           "none of the visitor's words (the customer field is the repo's masked email).", "", "```"]
    md += ["%s  %s" % (r["customer"], json.dumps(r["enquiry"])) if "enquiry" in r else "%s  (escalated before routing) %s" % (
        r["customer"], r["escalation_reason"]) for r in records]
    md += ["```", "", "## End state of the fake CRM", "",
           "%d leads, %d tasks, %d notes after five enquiries (scenario 5 added a note to scenario 1's lead, not a second lead)." % (
               len(zoho.leads), len(zoho.tasks), len(zoho.notes)), ""]
    print("\nFake CRM end state: %d leads, %d tasks, %d notes" % (len(zoho.leads), len(zoho.tasks), len(zoho.notes)))
    (HERE / "demo_enquiry_transcript.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
