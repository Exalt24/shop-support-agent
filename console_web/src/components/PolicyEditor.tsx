"use client";

import { useState } from "react";
import { CheckCircle2, Loader2, Save } from "lucide-react";
import { api } from "@/lib/client";

/** The policy is the only source the agent may quote. Saving is recorded in the audit log. */
export default function PolicyEditor({ store, initial }: { store: string; initial: string }) {
  const [text, setText] = useState(initial);
  const [saved, setSaved] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const dirty = text !== saved;

  async function save() {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await api(`/api/t/${store}/settings/policy`, "PUT", { policy_md: text });
      setSaved(text);
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <label htmlFor="policy" className="sr-only">Store policy</label>
      <textarea id="policy" value={text} onChange={(e) => { setText(e.target.value); setDone(false); }} rows={16} spellCheck
        className="w-full rounded-lg border border-line-strong bg-surface px-3.5 py-3 font-mono text-[13px] leading-relaxed outline-none focus:border-accent focus:ring-2 focus:ring-accent/25 resize-y" />
      <p className="mt-1.5 text-xs text-ink-3">One <code className="font-mono">## Heading</code> per topic, with the rule underneath. The agent answers only from what is written here.</p>
      {error && <p role="alert" className="mt-2 text-sm text-bad">{error}</p>}
      <div className="mt-3 flex items-center justify-end gap-3">
        {done && <span className="inline-flex items-center gap-1.5 text-sm text-ok"><CheckCircle2 size={16} aria-hidden />Saved. The agent uses it from the next message.</span>}
        <button type="button" onClick={save} disabled={busy || !dirty}
          className="h-10 rounded-lg bg-accent text-white px-4 text-sm font-medium inline-flex items-center gap-2 whitespace-nowrap hover:brightness-110 disabled:opacity-60">
          {busy ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <Save size={16} aria-hidden />}Save policy
        </button>
      </div>
    </div>
  );
}
