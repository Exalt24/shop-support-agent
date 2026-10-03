import type { Metadata } from "next";
import Link from "next/link";
import { ArrowRight, Bot, CheckCheck, Hand, MessagesSquare } from "lucide-react";
import { apiGet } from "@/lib/api";
import { ago, INTENT_LABEL, nameFromEmail, reasonText } from "@/lib/format";
import type { Dashboard, HandoffRow } from "@/lib/types";
import { Card, EmptyState, PageHeader, StatusChip } from "@/components/ui";

export const metadata: Metadata = { title: "Dashboard" };

function Stat({ icon, label, value, hint, tone }: { icon: React.ReactNode; label: string; value: string; hint: string; tone: string }) {
  return (
    <Card className="p-5">
      <div className="flex items-center gap-3">
        <span className={`grid place-items-center size-9 rounded-lg ${tone}`}>{icon}</span>
        <span className="text-sm font-medium text-ink-2">{label}</span>
      </div>
      <div className="mt-3 text-3xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 text-sm text-ink-3">{hint}</div>
    </Card>
  );
}

export default async function DashboardPage({ params }: PageProps<"/[store]">) {
  const { store } = await params;
  const [d, h] = await Promise.all([
    apiGet<Dashboard>(`/api/t/${store}/dashboard`),
    apiGet<{ items: HandoffRow[] }>(`/api/t/${store}/handoffs?status=active`),
  ]);
  const max = Math.max(1, ...d.days.map((x) => x.n));
  const waiting = d.handoffs.open + d.handoffs.claimed;
  const dayLabel = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { weekday: "short" });

  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-6xl">
      <PageHeader title={d.store} subtitle="The last 7 days, and what needs your team right now." />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
        <Stat icon={<MessagesSquare size={18} aria-hidden />} tone="bg-accent-soft text-accent" label="Conversations" value={String(d.week.total)} hint="started this week" />
        <Stat icon={<Bot size={18} aria-hidden />} tone="bg-ok-soft text-ok" label="Answered by the agent" value={d.agent_handled_pct === null ? "n/a" : `${d.agent_handled_pct}%`} hint="no person needed" />
        <Stat icon={<Hand size={18} aria-hidden />} tone="bg-warn-soft text-warn" label="Waiting for a person" value={String(waiting)} hint={`${d.handoffs.open} unclaimed, ${d.handoffs.claimed} in progress`} />
        <Stat icon={<CheckCheck size={18} aria-hidden />} tone="bg-done-soft text-done" label="Resolved by your team" value={String(d.week.resolved)} hint="this week" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)] gap-3 sm:gap-4 mt-3 sm:mt-4">
        <Card className="p-5">
          <h2 className="font-semibold">Conversations per day</h2>
          <div className="mt-6 flex items-end gap-2 sm:gap-3 h-44" role="img" aria-label={`Conversations per day: ${d.days.map((x) => `${dayLabel(x.day)} ${x.n}`).join(", ")}`}>
            {d.days.map((x) => (
              <div key={x.day} className="flex-1 flex flex-col items-center justify-end gap-2 h-full">
                <span className="text-xs font-medium text-ink-2">{x.n || ""}</span>
                <div className={`w-full rounded-t-md ${x.n ? "bg-accent" : "bg-surface-2"}`} style={{ height: `${Math.max(6, (x.n / max) * 100)}%` }} />
                <span className="text-xs text-ink-3">{dayLabel(x.day)}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card className="p-5">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">Needs your team</h2>
            <Link href={`/${store}/handoffs`} className="text-sm font-medium text-accent inline-flex items-center gap-1 hover:underline">All handoffs <ArrowRight size={14} aria-hidden /></Link>
          </div>
          {h.items.length === 0 ? (
            <EmptyState icon={<CheckCheck size={22} aria-hidden />} title="Nothing waiting" body="When the agent hands something to your team, it shows up here with everything it did." />
          ) : (
            <ul className="mt-3 divide-y divide-line">
              {h.items.slice(0, 4).map((x) => (
                <li key={x.id}>
                  <Link href={`/${store}/inbox?c=${x.conversation_id}`} className="block py-3 hover:bg-surface-2 -mx-2 px-2 rounded-lg">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium truncate">{nameFromEmail(x.customer_email)}</span>
                      <span className="text-xs text-ink-3 whitespace-nowrap">{ago(x.created_at)}</span>
                    </div>
                    <div className="text-sm text-warn mt-0.5">{reasonText(x.reason)}</div>
                    {x.preview && <div className="text-sm text-ink-2 truncate mt-0.5">{x.preview}</div>}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card className="mt-3 sm:mt-4">
        <div className="flex items-center justify-between p-5 pb-2">
          <h2 className="font-semibold">Recent conversations</h2>
          <Link href={`/${store}/inbox`} className="text-sm font-medium text-accent inline-flex items-center gap-1 hover:underline">Open inbox <ArrowRight size={14} aria-hidden /></Link>
        </div>
        <ul className="divide-y divide-line">
          {d.recent.map((c) => (
            <li key={c.id}>
              <Link href={`/${store}/inbox?c=${c.id}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 hover:bg-surface-2">
                <span className="font-medium w-44 truncate">{nameFromEmail(c.customer_email)}</span>
                <span className="text-sm text-ink-2 flex-1 min-w-32 truncate">{c.intent ? INTENT_LABEL[c.intent] : "Conversation"}{c.order_name ? ` · ${c.order_name}` : ""}</span>
                <StatusChip status={c.status} compact />
                <span className="text-xs text-ink-3 w-20 text-right whitespace-nowrap">{ago(c.updated_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
