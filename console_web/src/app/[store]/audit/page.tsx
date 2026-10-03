import type { Metadata } from "next";
import Link from "next/link";
import { ChevronLeft, ChevronRight, ScrollText } from "lucide-react";
import { apiGet } from "@/lib/api";
import { clock } from "@/lib/format";
import type { AuditEntry } from "@/lib/types";
import { Card, Chip, EmptyState, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Audit log" };

const ACTIONS: Record<string, string> = {
  "chat.reply": "Agent answered",
  "chat.escalated": "Agent handed off",
  "handoff.claim": "Case claimed",
  "handoff.resolve": "Case resolved",
  "lookup.order": "Order looked up",
  "policy.update": "Policy edited",
};
const FILTERS = [["", "Everything"], ["chat.escalated", "Handoffs"], ["handoff.resolve", "Resolutions"], ["lookup.order", "Order lookups"], ["policy.update", "Policy edits"]];

/** One plain sentence of detail per entry, so an admin never reads raw JSON. */
function summary(e: AuditEntry): string {
  const d = e.detail as Record<string, string | boolean | string[] | null | undefined>;
  const parts: string[] = [];
  if (d.order) parts.push(`order ${d.order}`);
  if (d.intent) parts.push(String(d.intent).replace(/_/g, " "));
  if (d.reason) parts.push(`reason: ${d.reason}`);
  if (d.found === false) parts.push("not found");
  if (Array.isArray(d.guard_violations) && d.guard_violations.length) parts.push(`${d.guard_violations.length} draft(s) blocked by the guard`);
  if (typeof d.chars === "number") parts.push(`${d.chars} characters`);
  return parts.join(" · ");
}

export default async function AuditPage({ params, searchParams }: PageProps<"/[store]/audit">) {
  const { store } = await params;
  const sp = await searchParams;
  const action = (Array.isArray(sp.action) ? sp.action[0] : sp.action) ?? "";
  const page = Math.max(1, Number(Array.isArray(sp.page) ? sp.page[0] : sp.page) || 1);
  const qs = new URLSearchParams({ page: String(page), per: "30" });
  if (action) qs.set("action", action);
  const log = await apiGet<{ items: AuditEntry[]; total: number; page: number; per: number }>(`/api/t/${store}/audit?${qs}`);
  const pages = Math.max(1, Math.ceil(log.total / log.per));
  const link = (p: number) => `/${store}/audit?${new URLSearchParams({ ...(action ? { action } : {}), page: String(p) })}`;

  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-4xl">
      <PageHeader title="Audit log" subtitle="Everything the agent and your team did in this store. Entries can be read, never edited or deleted." />
      <nav aria-label="Filter the log" className="flex flex-wrap gap-1.5 mb-4">
        {FILTERS.map(([k, label]) => (
          <Link key={k} href={`/${store}/audit${k ? `?action=${k}` : ""}`} aria-current={action === k ? "page" : undefined}
            className={`h-9 px-3.5 grid place-items-center rounded-full text-sm font-medium border ${action === k ? "bg-ink text-white border-ink" : "bg-surface text-ink-2 border-line hover:border-line-strong"}`}>
            {label}
          </Link>
        ))}
      </nav>
      <Card>
        {log.items.length === 0 ? (
          <EmptyState icon={<ScrollText size={22} aria-hidden />} title="No entries" body="Nothing matches this filter yet." />
        ) : (
          <ul className="divide-y divide-line">
            {log.items.map((e) => (
              <li key={e.id} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 px-4 sm:px-5 py-3">
                <span className="text-xs text-ink-3 w-28 shrink-0 whitespace-nowrap">{clock(e.created_at)}</span>
                <Chip tone={e.action === "chat.escalated" ? "warn" : "neutral"}>{ACTIONS[e.action] ?? e.action}</Chip>
                <span className="text-sm text-ink-2 flex-1 min-w-40">{summary(e) || <span className="text-ink-3">no detail</span>}</span>
                <span className="text-xs text-ink-3 truncate max-w-48">{e.actor}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {pages > 1 && (
        <div className="mt-4 flex items-center justify-between text-sm text-ink-2">
          <span>Page {log.page} of {pages} · {log.total} entries</span>
          <span className="flex gap-2">
            {log.page > 1 && <Link href={link(log.page - 1)} className="h-9 px-3 rounded-lg border border-line bg-surface inline-flex items-center gap-1 hover:border-line-strong"><ChevronLeft size={15} aria-hidden />Newer</Link>}
            {log.page < pages && <Link href={link(log.page + 1)} className="h-9 px-3 rounded-lg border border-line bg-surface inline-flex items-center gap-1 hover:border-line-strong">Older<ChevronRight size={15} aria-hidden /></Link>}
          </span>
        </div>
      )}
    </div>
  );
}
