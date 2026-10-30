"""Lead capture: turn a consented enquiry into a Zoho Lead, a Task for the owner queue and a Note, once.

The rules, each pinned by a test and a mutant:
  * One lead per contact per tenant per day. The idempotency key is a hash of tenant, the visitor's email (or phone when there
    is no email) and the date, so a retried request, a double click or a second enquiry the same day finds the first one. A
    second, different enquiry adds a Note to that lead and nothing else; an identical retry adds nothing.
  * Progress is written to the ledger after every call, so a request that failed halfway resumes (the Task, then the Note)
    and never upserts or creates twice. The upsert also checks Email at Zoho, so across days the same person is updated,
    not duplicated.
  * Failure never changes what the visitor is told. capture() does not raise: it returns an outcome with the vendor's code,
    the caller records it, and the reply was already decided before the CRM was called.
  * The Note is built here, from the visitor's own words after summarise() (cards, emails, phones and secret sentences
    removed, shortened). Nothing the model wrote reaches the CRM except values that passed read_fields().

The ledger is in memory, one process: a restart forgets it. The Email duplicate check at Zoho still stops a second lead; a note
or task could repeat once. A real deployment would put the ledger in the database next to the tenant.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .enquiry import ROUTES, EnquiryDesk, Route, add_business_days, split_name
from .zoho_crm import CrmClient, ZohoCrmClient, ZohoError, ZohoSettings, scrub


def idempotency_key(tenant: str, contact: str, day: date) -> str:
    return hashlib.sha256(("%s|%s|%s" % (tenant, contact.strip().lower(), day.isoformat())).encode()).hexdigest()[:32]


def _note_key(intent: str, summary: str) -> str:
    return hashlib.sha256(("%s|%s" % (intent, summary)).encode()).hexdigest()[:12]


@dataclass
class LedgerEntry:
    day: date
    lead_id: str | None = None
    action: str | None = None
    task_id: str | None = None
    notes: set[str] = field(default_factory=set)


class LeadLedger:
    def __init__(self):
        self._items: dict[str, LedgerEntry] = {}

    def get(self, key: str) -> LedgerEntry | None:
        return self._items.get(key)

    def open(self, key: str, day: date) -> LedgerEntry:
        for k in [k for k, e in self._items.items() if (day - e.day).days > 1]:
            del self._items[k]  # yesterday's keys can never match again
        return self._items.setdefault(key, LedgerEntry(day))


@dataclass(frozen=True)
class LeadOutcome:
    status: str          # created, updated, resumed, noted, duplicate, failed
    vendor_code: str     # SUCCESS, or the vendor's error code, or TRANSPORT / UNEXPECTED
    fields_sent: int = 0
    detail: str = ""     # failure only: the vendor's message with any email masked
    lead_id: str | None = None  # the CRM's id for the lead, once there is one (the MCP tools hand it on)


class LeadCapture:
    def __init__(self, crm: CrmClient, ledger: LeadLedger | None = None, *, tenant: str = "default",
                 assignment_rules: dict[str, str] | None = None, today: Callable[[], date] = date.today,
                 routes: dict[str, Route] | None = None):
        self._crm, self._ledger, self._tenant = crm, ledger or LeadLedger(), tenant
        self._routes = routes or ROUTES  # the tenant's own owner queues; the default table when none are given
        self._rules, self._today = assignment_rules or {}, today
        self._lock = threading.Lock()

    @staticmethod
    def lead_record(fields: dict[str, str]) -> dict:
        first, last = split_name(fields["name"])
        rec = {"Last_Name": last}
        if first:
            rec["First_Name"] = first
        for src, dst in (("email", "Email"), ("phone", "Phone"), ("company", "Company")):
            if fields.get(src):
                rec[dst] = fields[src]
        return rec

    def capture(self, fields: dict[str, str], intent: str, summary: str, *, channel: str = "Web chat",
                consent: str | None = None) -> LeadOutcome:
        record = self.lead_record(fields)
        route = self._routes[intent]
        with self._lock:  # one capture at a time: two identical requests cannot both pass the ledger check
            day = self._today()
            key = idempotency_key(self._tenant, fields.get("email") or fields.get("phone") or "", day)
            entry = self._ledger.open(key, day)
            note_key = _note_key(intent, summary)
            did = {"upsert": False, "task": False, "note": False}
            sent = len(record)
            try:
                if not entry.lead_id:
                    dup = ["Email"] if "Email" in record else []
                    up = self._crm.upsert_lead(record, dup)
                    entry.lead_id, entry.action = up.id, up.action
                    did["upsert"] = True
                if route.task_subject and not entry.task_id:
                    task = {"Subject": route.task_subject, "Status": "Not Started", "Priority": route.priority,
                            "Due_Date": add_business_days(day, route.due_days).isoformat(),
                            "What_Id": {"id": entry.lead_id}, "$se_module": "Leads"}
                    entry.task_id = self._crm.create_task(task, self._rules.get(route.queue)) or "created"
                    did["task"] = True
                if note_key not in entry.notes:
                    content = "%s enquiry (%s, %s queue). The visitor wrote: \"%s\"" % (channel, intent.replace("_", " "), route.queue, summary)
                    if consent:  # how the yes was given, recorded where the team will read it
                        content += " Consent recorded: %s." % consent
                    self._crm.add_note("Leads", entry.lead_id, content)
                    entry.notes.add(note_key)
                    did["note"] = True
            except ZohoError as e:
                return LeadOutcome("failed", e.code, sent, scrub(e), entry.lead_id)
            except Exception as e:  # lead capture is a convenience for the team: it must never cost the visitor an answer
                return LeadOutcome("failed", "UNEXPECTED", sent, type(e).__name__, entry.lead_id)
            if did["upsert"]:
                status = "created" if entry.action == "insert" else "updated"
            elif did["task"]:
                status = "resumed"
            elif did["note"]:
                status = "noted"
            else:
                status = "duplicate"
            return LeadOutcome(status, "SUCCESS", sent if did["upsert"] else 0, "", entry.lead_id)


def desk_from_env(env: dict[str, str] | None = None, transport=None) -> EnquiryDesk | None:
    """The desk for serve.py: None unless ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN are all set, so a deployment
    with no CRM behaves exactly as before. Also read: ZOHO_REGION (us, au, eu, in, jp, ca, cn), SUPPORT_TENANT, and
    ZOHO_ASSIGNMENT_RULES as `sales=<lar_id>,service=<lar_id>` (an optional Zoho assignment rule per queue)."""
    import os
    env = os.environ if env is None else env
    if not all(env.get(n) for n in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")):
        return None
    tenant = env.get("SUPPORT_TENANT") or "default"
    crm = ZohoCrmClient(ZohoSettings.from_env(env), transport)
    return EnquiryDesk(LeadCapture(crm, tenant=tenant, assignment_rules=rules_from_env(env)), tenant=tenant)


def rules_from_env(env: dict[str, str]) -> dict[str, str]:
    """ZOHO_ASSIGNMENT_RULES as `sales=<lar_id>,service=<lar_id>`: only the default queues, only with a rule id."""
    rules = {}
    for pair in (env.get("ZOHO_ASSIGNMENT_RULES") or "").split(","):
        queue, _, rule = pair.partition("=")
        if queue.strip() in {r.queue for r in ROUTES.values()} and rule.strip():
            rules[queue.strip()] = rule.strip()
    return rules
