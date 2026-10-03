"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { clock, INTENT_LABEL, reasonText } from "@/lib/format";
import type { ConversationDetail, Meta, OrderView } from "@/lib/types";
import OrderCard from "@/components/OrderCard";
import AgentTrace from "./AgentTrace";

function Section({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="border-b border-line last:border-b-0">
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-3 text-[11px] font-semibold uppercase tracking-wider text-ink-3 hover:text-ink">
        {title}
        <ChevronDown size={15} className={`transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && <div className="px-4 pb-4">{children}</div>}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1 text-sm">
      <span className="text-ink-3 shrink-0">{label}</span>
      <span className="text-right min-w-0 truncate">{children}</span>
    </div>
  );
}

/** Context beside the thread (Intercom's "Details" pattern): the customer, the live order, the handoff, the agent's trace.
 * The order is fetched once on the server and passed in, so showing this panel differently per screen size never
 * triggers a second lookup (and a second audit entry). */
export default function SidePanel({ detail, order, orderError }: { detail: ConversationDetail; order?: OrderView; orderError?: string }) {
  const { conversation: c, handoff, messages } = detail;
  const lastAgent: Meta | undefined = [...messages].reverse().find((m) => m.sender === "agent")?.meta;

  return (
    <div>
      <Section title="Customer">
        <Row label="Email">{c.customer_email}</Row>
        <Row label="Channel"><span className="capitalize">{c.channel}</span></Row>
        <Row label="Topic">{c.intent ? INTENT_LABEL[c.intent] ?? c.intent : "Not classified"}</Row>
        <Row label="Started">{clock(c.created_at)}</Row>
      </Section>

      {c.order_name && (
        <Section title={`Order ${c.order_name}`}>
          {orderError ? <p role="alert" className="rounded-lg bg-warn-soft text-warn px-3 py-2 text-sm">{orderError}</p>
            : order ? <OrderCard order={order} /> : null}
          <p className="mt-2 text-xs text-ink-3">Looked up live from the store. Opening it is recorded in the audit log.</p>
        </Section>
      )}

      {handoff && (
        <Section title="Handoff">
          <Row label="Why">{reasonText(handoff.reason)}</Row>
          <Row label="Opened">{clock(handoff.created_at)}</Row>
          <Row label="Owner">{handoff.claimed_by_name ?? "Unclaimed"}</Row>
          <Row label="Status"><span className="capitalize">{handoff.status}</span></Row>
        </Section>
      )}

      {lastAgent && (
        <Section title="How the agent answered">
          <AgentTrace meta={lastAgent} defaultOpen label="Steps in order" />
          {lastAgent.guard_violations && lastAgent.guard_violations.length > 0 && (
            <p className="mt-2 text-xs text-ink-3">The guard blocked {lastAgent.guard_violations.length} draft{lastAgent.guard_violations.length > 1 ? "s" : ""} before anything reached the customer.</p>
          )}
        </Section>
      )}
    </div>
  );
}
