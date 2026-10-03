import Link from "next/link";
import { ChevronLeft, ChevronRight, Inbox } from "lucide-react";
import { ago, INTENT_LABEL, initials, nameFromEmail } from "@/lib/format";
import type { ConversationRow } from "@/lib/types";
import { Avatar, Chip, EmptyState, StatusChip } from "@/components/ui";

interface Props {
  store: string;
  items: ConversationRow[];
  total: number;
  page: number;
  per: number;
  selected?: string;
  filters: { q: string; status: string; intent: string };
}

/** Plain links, not a listbox: each row navigates, which is what a link is for. The open row is aria-current. */
export default function ConversationList({ store, items, total, page, per, selected, filters }: Props) {
  const href = (extra: Record<string, string | number | undefined>) => {
    const p = new URLSearchParams();
    if (filters.q) p.set("q", filters.q);
    if (filters.status) p.set("status", filters.status);
    if (filters.intent) p.set("intent", filters.intent);
    for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== "") p.set(k, String(v));
    return `/${store}/inbox?${p.toString()}`;
  };
  const from = total === 0 ? 0 : (page - 1) * per + 1;
  const to = Math.min(page * per, total);
  const filtered = Boolean(filters.q || filters.status || filters.intent);

  if (items.length === 0) {
    return (
      <EmptyState icon={<Inbox size={22} aria-hidden />}
        title={filtered ? "Nothing matches" : "No conversations yet"}
        body={filtered ? "Try a different search or clear a filter." : "When a customer writes in, the conversation appears here, answered or handed to your team."}
        action={filtered ? <Link href={`/${store}/inbox`} className="text-sm font-medium text-accent hover:underline">Clear all filters</Link> : undefined} />
    );
  }

  return (
    <>
      <ul className="divide-y divide-line">
        {items.map((c) => {
          const isSel = c.id === selected;
          const name = nameFromEmail(c.customer_email);
          return (
            <li key={c.id}>
              <Link href={href({ c: c.id, page: page > 1 ? page : undefined })} aria-current={isSel ? "true" : undefined}
                className={`block px-4 py-3.5 border-l-[3px] transition-colors ${isSel ? "bg-accent-soft border-accent" : "border-transparent hover:bg-surface-2"}`}>
                <div className="flex items-start gap-3">
                  <Avatar label={initials(c.customer_email)} tone={isSel ? "accent" : "neutral"} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-semibold truncate">{name}</span>
                      <span className="text-xs text-ink-3 whitespace-nowrap">{ago(c.updated_at)}</span>
                    </div>
                    <p className="text-sm text-ink-2 line-clamp-2 mt-0.5">{c.preview ?? "No messages"}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <StatusChip status={c.status} compact />
                      {c.intent && <Chip>{INTENT_LABEL[c.intent] ?? c.intent}</Chip>}
                      {c.order_name && <Chip tone="accent">{c.order_name}</Chip>}
                      {c.assignee && <Chip>{c.assignee}</Chip>}
                    </div>
                  </div>
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
      <nav aria-label="Pages" className="flex items-center justify-between px-4 py-3 border-t border-line text-sm text-ink-2 bg-surface sticky bottom-0">
        <span>{from}-{to} of {total}</span>
        <span className="flex gap-1">
          {page > 1 ? (
            <Link href={href({ page: page - 1 })} aria-label="Previous page" className="grid place-items-center size-9 rounded-lg hover:bg-surface-2"><ChevronLeft size={18} aria-hidden /></Link>
          ) : <span className="grid place-items-center size-9 text-ink-3/50"><ChevronLeft size={18} aria-hidden /></span>}
          {to < total ? (
            <Link href={href({ page: page + 1 })} aria-label="Next page" className="grid place-items-center size-9 rounded-lg hover:bg-surface-2"><ChevronRight size={18} aria-hidden /></Link>
          ) : <span className="grid place-items-center size-9 text-ink-3/50"><ChevronRight size={18} aria-hidden /></span>}
        </span>
      </nav>
    </>
  );
}
