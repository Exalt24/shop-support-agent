import type { Metadata } from "next";
import Link from "next/link";
import { MessagesSquare, X } from "lucide-react";
import { apiGet, apiTry } from "@/lib/api";
import type { ConversationDetail as Detail, ConversationRow, Me, OrderView } from "@/lib/types";
import ConversationList from "@/components/inbox/ConversationList";
import ConversationDetail from "@/components/inbox/ConversationDetail";
import ListFilters from "@/components/inbox/ListFilters";
import SidePanel from "@/components/inbox/SidePanel";
import { EmptyState } from "@/components/ui";

export const metadata: Metadata = { title: "Inbox" };

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? "";

export default async function InboxPage({ params, searchParams }: PageProps<"/[store]/inbox">) {
  const { store } = await params;
  const sp = await searchParams;
  const q = one(sp.q), status = one(sp.status), intent = one(sp.intent), c = one(sp.c);
  const showDetails = one(sp.d) === "1";
  const page = Math.max(1, Number(one(sp.page)) || 1);

  const qs = new URLSearchParams({ page: String(page), per: "20" });
  if (q) qs.set("q", q);
  if (status) qs.set("status", status);
  if (intent) qs.set("intent", intent);

  const [list, me] = await Promise.all([
    apiGet<{ items: ConversationRow[]; total: number; page: number; per: number }>(`/api/t/${store}/conversations?${qs}`),
    apiGet<Me>("/api/me"),
  ]);
  const role = me.stores.find((s) => s.slug === store)?.role ?? "agent";

  let detail: Detail | null = null;
  let order: OrderView | undefined;
  let orderError: string | undefined;
  if (c) {
    detail = await apiGet<Detail>(`/api/t/${store}/conversations/${c}`);
    if (detail.conversation.order_name) {
      const r = await apiTry<OrderView>(`/api/t/${store}/orders/${detail.conversation.order_name.replace("#", "")}`);
      order = r.data;
      orderError = r.error;
    }
  }

  const keep = new URLSearchParams();
  if (q) keep.set("q", q);
  if (status) keep.set("status", status);
  if (intent) keep.set("intent", intent);
  if (page > 1) keep.set("page", String(page));
  const backHref = `/${store}/inbox${keep.toString() ? `?${keep}` : ""}`;
  const detailsHref = (on: boolean) => {
    const p = new URLSearchParams(keep);
    p.set("c", c);
    if (on) p.set("d", "1");
    return `/${store}/inbox?${p}`;
  };

  return (
    <div className="h-[calc(100dvh-3.5rem)] lg:h-dvh grid grid-cols-[minmax(0,1fr)] lg:grid-cols-[340px_minmax(0,1fr)] 2xl:grid-cols-[360px_minmax(0,1fr)_340px] bg-surface">
      <section className={`${c ? "hidden lg:flex" : "flex"} flex-col min-h-0 border-r border-line`} aria-label="Conversations">
        <div className="shrink-0 p-4 border-b border-line">
          <h1 className="text-xl font-semibold tracking-tight mb-3">Inbox</h1>
          <ListFilters q={q} status={status} intent={intent} />
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto pane-scroll">
          <ConversationList store={store} items={list.items} total={list.total} page={list.page} per={list.per} selected={c} filters={{ q, status, intent }} />
        </div>
      </section>

      <section className={`${c ? "flex" : "hidden lg:flex"} flex-col min-h-0 relative`} aria-label="Conversation">
        {detail ? (
          <ConversationDetail detail={detail} store={store} meId={me.user.id} role={role} backHref={backHref} detailsHref={detailsHref(true)} />
        ) : (
          <div className="flex-1 grid place-items-center bg-bg">
            <EmptyState icon={<MessagesSquare size={22} aria-hidden />} title="Pick a conversation"
              body="Choose one from the list to read it, see what the agent looked up, and reply if it was handed to your team." />
          </div>
        )}
      </section>

      {detail && (
        <aside aria-label="Details"
          className={`${showDetails ? "fixed inset-0 lg:left-auto lg:w-[380px] lg:shadow-2xl lg:border-l lg:border-line-strong z-30 bg-surface overflow-y-auto pane-scroll" : "hidden"} 2xl:static 2xl:block 2xl:w-auto 2xl:shadow-none 2xl:border-l 2xl:border-line 2xl:overflow-y-auto 2xl:min-h-0 2xl:bg-surface`}>
          <div className="2xl:hidden sticky top-0 z-10 flex items-center justify-between h-14 px-4 bg-surface border-b border-line">
            <span className="font-semibold">Details</span>
            <Link href={detailsHref(false)} aria-label="Close details" className="grid place-items-center size-10 -mr-2 rounded-lg hover:bg-surface-2"><X size={20} aria-hidden /></Link>
          </div>
          <SidePanel detail={detail} order={order} orderError={orderError} />
        </aside>
      )}

    </div>
  );
}
