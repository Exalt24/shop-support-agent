"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, CheckCheck, Loader2, UserCheck } from "lucide-react";
import { api } from "@/lib/client";
import { ago, INTENT_LABEL, nameFromEmail, reasonText } from "@/lib/format";
import type { HandoffRow } from "@/lib/types";
import { Avatar, Card, Chip } from "@/components/ui";
import { initials } from "@/lib/format";

/** One handoff in the queue. Claiming here is the same action as in the thread, so a person can take a case without opening it. */
export default function HandoffCard({ store, item, meId }: { store: string; item: HandoffRow; meId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mine = item.claimed_by === meId;
  const resolved = item.status === "resolved";

  async function claim() {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/t/${store}/handoffs/${item.id}/claim`, "POST");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <Avatar label={initials(item.customer_email)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-x-3">
            <span className="font-semibold truncate">{nameFromEmail(item.customer_email)}</span>
            <span className="text-xs text-ink-3 whitespace-nowrap">{ago(item.created_at)}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Chip tone={resolved ? "ok" : "warn"}>{reasonText(item.reason)}</Chip>
            {item.intent && <Chip>{INTENT_LABEL[item.intent] ?? item.intent}</Chip>}
            {item.order_name && <Chip>{item.order_name}</Chip>}
          </div>
          {item.preview && <p className="mt-2.5 text-ink-2 line-clamp-2">&ldquo;{item.preview}&rdquo;</p>}

          <div className="mt-3.5 flex flex-wrap items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-sm text-ink-2">
              {resolved ? <><CheckCheck size={15} className="text-done" aria-hidden />Resolved{item.claimed_by_name ? ` by ${item.claimed_by_name}` : ""}</>
                : item.claimed_by ? <><UserCheck size={15} aria-hidden />{mine ? "You have this case" : `${item.claimed_by_name} has this case`}</>
                : "Nobody has this yet"}
            </span>
            <div className="flex gap-2">
              {!resolved && !item.claimed_by && (
                <button type="button" onClick={claim} disabled={busy}
                  className="h-10 rounded-lg border border-line-strong bg-surface px-3.5 text-sm font-medium whitespace-nowrap inline-flex items-center gap-2 hover:bg-surface-2 disabled:opacity-60">
                  {busy ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <UserCheck size={16} aria-hidden />}Claim
                </button>
              )}
              <Link href={`/${store}/inbox?c=${item.conversation_id}`}
                className="h-10 rounded-lg bg-ink text-white px-3.5 text-sm font-medium whitespace-nowrap inline-flex items-center gap-2 hover:opacity-90">
                Open case<ArrowRight size={16} aria-hidden />
              </Link>
            </div>
          </div>
          {error && <p role="alert" className="mt-2 text-sm text-bad">{error}</p>}
        </div>
      </div>
    </Card>
  );
}
