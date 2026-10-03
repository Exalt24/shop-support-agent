"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCheck, Hand, Loader2, Send, UserCheck } from "lucide-react";
import { api } from "@/lib/client";
import { clock, reasonText } from "@/lib/format";
import type { Handoff, Role } from "@/lib/types";

interface Props { store: string; handoff: Handoff; meId: string; role: Role }

/** One clear owner per case: claim first, then reply and resolve. Someone else's case is read-only (admins can override). */
export default function ReplyBox({ store, handoff, meId, role }: Props) {
  const router = useRouter();
  const [reply, setReply] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"claim" | "resolve" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: "claim" | "resolve") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "claim") await api(`/api/t/${store}/handoffs/${handoff.id}/claim`, "POST");
      else await api(`/api/t/${store}/handoffs/${handoff.id}/resolve`, "POST", { reply, note: note || null });
      router.refresh();
      setReply("");
      setNote("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(null);
    }
  }

  if (handoff.status === "resolved") {
    return (
      <div className="rounded-xl bg-done-soft text-done px-4 py-3 text-sm flex gap-3">
        <CheckCheck size={18} className="mt-0.5 shrink-0" aria-hidden />
        <div>
          <div className="font-medium">Resolved {handoff.resolved_at ? clock(handoff.resolved_at) : ""}{handoff.claimed_by_name ? ` by ${handoff.claimed_by_name}` : ""}</div>
          {handoff.resolution_note && <div className="mt-0.5">Internal note: {handoff.resolution_note}</div>}
        </div>
      </div>
    );
  }

  const mine = handoff.claimed_by === meId;
  const others = handoff.claimed_by && !mine;

  if (!handoff.claimed_by) {
    return (
      <div className="rounded-xl border border-warn/30 bg-warn-soft px-4 py-3.5">
        <div className="flex items-start gap-3">
          <Hand size={18} className="mt-0.5 text-warn shrink-0" aria-hidden />
          <div className="flex-1">
            <div className="font-medium text-warn">The agent handed this to your team</div>
            <div className="text-sm text-warn/90">{reasonText(handoff.reason)}</div>
          </div>
          <button type="button" onClick={() => run("claim")} disabled={busy !== null}
            className="h-10 rounded-lg bg-ink text-white px-4 text-sm font-medium whitespace-nowrap inline-flex items-center gap-2 hover:opacity-90 disabled:opacity-60">
            {busy === "claim" ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <UserCheck size={16} aria-hidden />}
            Claim this case
          </button>
        </div>
        {error && <p role="alert" className="mt-2 text-sm text-bad">{error}</p>}
      </div>
    );
  }

  if (others && role !== "admin") {
    return (
      <div className="rounded-xl bg-surface-2 text-ink-2 px-4 py-3 text-sm flex gap-3">
        <UserCheck size={18} className="mt-0.5 shrink-0" aria-hidden />
        <div><span className="font-medium text-ink">{handoff.claimed_by_name}</span> has this case. You can read it, but only they can reply.</div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-line-strong bg-surface p-3.5">
      <label htmlFor="reply" className="flex items-center gap-2 text-sm font-medium mb-2">
        <UserCheck size={16} className="text-accent" aria-hidden />
        {others ? `Reply as admin (${handoff.claimed_by_name} has this case)` : "Your reply to the customer"}
      </label>
      <textarea id="reply" value={reply} onChange={(e) => setReply(e.target.value)} rows={4} placeholder="Write the reply the customer will see"
        className="w-full rounded-lg border border-line-strong bg-surface px-3.5 py-2.5 outline-none focus:border-accent focus:ring-2 focus:ring-accent/25 resize-y" />
      <label htmlFor="note" className="block text-xs font-medium text-ink-3 mt-3 mb-1">Internal note (optional, only your team sees it)</label>
      <input id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What you did, for the next person"
        className="w-full h-10 rounded-lg border border-line-strong bg-surface px-3.5 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/25" />
      {error && <p role="alert" className="mt-2 text-sm text-bad">{error}</p>}
      <div className="mt-3 flex justify-end">
        <button type="button" onClick={() => run("resolve")} disabled={busy !== null || !reply.trim()}
          className="h-10 rounded-lg bg-accent text-white px-4 text-sm font-medium whitespace-nowrap inline-flex items-center gap-2 hover:brightness-110 active:scale-[0.99] disabled:opacity-60">
          {busy === "resolve" ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <Send size={16} aria-hidden />}
          Send reply and resolve
        </button>
      </div>
    </div>
  );
}
