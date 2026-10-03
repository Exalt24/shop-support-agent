import type { Metadata } from "next";
import Link from "next/link";
import { CheckCheck } from "lucide-react";
import { apiGet } from "@/lib/api";
import type { HandoffRow, Me } from "@/lib/types";
import HandoffCard from "@/components/HandoffCard";
import { EmptyState, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Handoffs" };

const TABS = [
  { key: "active", label: "Waiting" },
  { key: "resolved", label: "Resolved" },
  { key: "all", label: "All" },
];

export default async function HandoffsPage({ params, searchParams }: PageProps<"/[store]/handoffs">) {
  const { store } = await params;
  const sp = await searchParams;
  const raw = Array.isArray(sp.status) ? sp.status[0] : sp.status;
  const status = TABS.some((t) => t.key === raw) ? (raw as string) : "active";

  const [list, me] = await Promise.all([
    apiGet<{ items: HandoffRow[] }>(`/api/t/${store}/handoffs?status=${status}`),
    apiGet<Me>("/api/me"),
  ]);

  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-3xl">
      <PageHeader title="Handoffs" subtitle="Cases the agent passed to a person, with the reason it gave." />
      <nav aria-label="Filter handoffs" className="flex gap-1 p-1 rounded-xl bg-surface-2 w-fit mb-5">
        {TABS.map((t) => (
          <Link key={t.key} href={`/${store}/handoffs?status=${t.key}`} aria-current={status === t.key ? "page" : undefined}
            className={`h-9 px-4 grid place-items-center rounded-lg text-sm font-medium ${status === t.key ? "bg-surface shadow-sm text-ink" : "text-ink-2 hover:text-ink"}`}>
            {t.label}
          </Link>
        ))}
      </nav>
      {list.items.length === 0 ? (
        <EmptyState icon={<CheckCheck size={22} aria-hidden />} title={status === "resolved" ? "Nothing resolved yet" : "Nothing waiting"}
          body={status === "resolved" ? "Cases your team finishes will be listed here." : "When the agent hands something to your team it appears here."} />
      ) : (
        <ul className="space-y-3">
          {list.items.map((h) => <li key={h.id}><HandoffCard store={store} item={h} meId={me.user.id} /></li>)}
        </ul>
      )}
    </div>
  );
}
