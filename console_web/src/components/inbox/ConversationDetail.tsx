import Link from "next/link";
import { ArrowLeft, Bot, Hand, PanelRight, UserCheck } from "lucide-react";
import { clock, initials, nameFromEmail, reasonText } from "@/lib/format";
import type { ConversationDetail as Detail, Message, Role } from "@/lib/types";
import { Avatar, StatusChip } from "@/components/ui";
import AgentTrace from "./AgentTrace";
import ReplyBox from "./ReplyBox";

function dayLabel(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const same = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  const y = new Date(today.getTime() - 86400000);
  if (same(d, today)) return "Today";
  if (same(d, y)) return "Yesterday";
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

function Event({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex justify-center my-2">
      <span className="inline-flex items-center gap-1.5 rounded-full bg-surface-2 text-ink-2 px-3 py-1 text-xs font-medium">{icon}{children}</span>
    </div>
  );
}

function Bubble({ m, customerName }: { m: Message; customerName: string }) {
  const time = new Date(m.created_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  if (m.sender === "customer") {
    return (
      <div className="flex gap-2.5 max-w-[88%] sm:max-w-[75%]">
        <Avatar label={initials(customerName.replace(" ", "."))} />
        <div>
          <div className="text-xs text-ink-3 mb-1">{customerName} · {time}</div>
          <div className="rounded-2xl rounded-tl-md bg-surface border border-line px-4 py-2.5 whitespace-pre-wrap">{m.body}</div>
        </div>
      </div>
    );
  }
  const agent = m.sender === "agent";
  return (
    <div className="flex flex-row-reverse gap-2.5 max-w-[92%] sm:max-w-[80%] ml-auto">
      <span className={`grid place-items-center size-8 shrink-0 rounded-full ${agent ? "bg-accent text-white" : "bg-ink text-white"}`}>
        {agent ? <Bot size={16} aria-hidden /> : <UserCheck size={16} aria-hidden />}
      </span>
      <div className="min-w-0">
        <div className="text-xs text-ink-3 mb-1 text-right">{agent ? "Agent" : `${m.author ?? "Team"} (your team)`} · {time}</div>
        <div className={`rounded-2xl rounded-tr-md px-4 py-2.5 whitespace-pre-wrap border ${agent ? "bg-accent-soft border-accent-line" : "bg-surface border-line-strong"}`}>{m.body}</div>
        {agent && <AgentTrace meta={m.meta} />}
      </div>
    </div>
  );
}

export default function ConversationDetail({ detail, store, meId, role, backHref, detailsHref }: { detail: Detail; store: string; meId: string; role: Role; backHref: string; detailsHref: string }) {
  const { conversation: c, messages, handoff } = detail;
  const name = nameFromEmail(c.customer_email);
  let lastDay = "";
  return (
    <div className="flex flex-col min-h-0 h-full">
      <header className="flex items-center gap-3 px-4 sm:px-6 h-16 shrink-0 border-b border-line bg-surface">
        <Link href={backHref} aria-label="Back to the list" className="lg:hidden grid place-items-center size-10 -ml-2 rounded-lg hover:bg-surface-2"><ArrowLeft size={20} aria-hidden /></Link>
        <Avatar label={initials(c.customer_email)} tone="accent" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold truncate leading-tight">{name}</div>
          <div className="text-sm text-ink-3 truncate">{c.customer_email}</div>
        </div>
        {c.assignee && (
          <span className="hidden sm:inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-3 py-1 text-xs font-medium text-ink-2">
            <UserCheck size={13} aria-hidden />Assigned to {c.assignee}
          </span>
        )}
        <StatusChip status={c.status} />
        <Link href={detailsHref} aria-label="Show details" title="Details"
          className="2xl:hidden grid place-items-center size-10 -mr-2 shrink-0 rounded-lg text-ink-2 hover:bg-surface-2">
          <PanelRight size={20} aria-hidden />
        </Link>
      </header>

      <div className="flex-1 min-h-0 overflow-y-auto pane-scroll px-4 sm:px-6 py-5 space-y-4 bg-bg">
        {messages.map((m) => {
          const label = dayLabel(m.created_at);
          const showDay = label !== lastDay;
          lastDay = label;
          const escalated = m.sender === "agent" && m.meta?.action === "escalated";
          return (
            <div key={m.id} className="space-y-3">
              {showDay && <div className="text-center text-xs font-medium text-ink-3 py-1">{label}</div>}
              <Bubble m={m} customerName={name} />
              {escalated && <Event icon={<Hand size={13} aria-hidden />}>Handed to your team: {reasonText(m.meta.escalation_reason)}</Event>}
            </div>
          );
        })}
        {handoff?.claimed_by_name && handoff.status !== "open" && (
          <Event icon={<UserCheck size={13} aria-hidden />}>{handoff.claimed_by_name} took this case{handoff.created_at ? ` · ${clock(handoff.created_at)}` : ""}</Event>
        )}
      </div>

      {handoff && (
        <div className="shrink-0 border-t border-line bg-surface p-3 sm:p-4">
          <ReplyBox store={store} handoff={handoff} meId={meId} role={role} />
        </div>
      )}
    </div>
  );
}
