"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Search, X } from "lucide-react";

const STATUS = [
  { value: "", label: "All" },
  { value: "escalated", label: "Needs a person" },
  { value: "answered", label: "Answered" },
  { value: "resolved", label: "Resolved" },
];
const INTENTS = [
  { value: "", label: "Any topic" },
  { value: "order_status", label: "Order status" },
  { value: "return_request", label: "Returns" },
  { value: "policy_question", label: "Policy questions" },
  { value: "product_question", label: "Product questions" },
  { value: "other", label: "Other" },
];

export default function ListFilters({ q, status, intent }: { q: string; status: string; intent: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const [text, setText] = useState(q);
  const first = useRef(true);

  const go = (next: { q?: string; status?: string; intent?: string }) => {
    const p = new URLSearchParams();
    const v = { q, status, intent, ...next };
    if (v.q) p.set("q", v.q);
    if (v.status) p.set("status", v.status);
    if (v.intent) p.set("intent", v.intent);
    const s = p.toString();
    router.push(s ? `${pathname}?${s}` : pathname);
  };

  // search as you type, after a short pause, so the list is not re-fetched on every keystroke
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const t = setTimeout(() => { if (text !== q) go({ q: text }); }, 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  return (
    <div className="space-y-3">
      <div className="relative">
        <Search size={17} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" aria-hidden />
        <input
          type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Search conversations"
          aria-label="Search conversations"
          className="w-full h-11 rounded-lg border border-line-strong bg-surface pl-10 pr-10 outline-none focus:border-accent focus:ring-2 focus:ring-accent/25"
        />
        {text && (
          <button type="button" onClick={() => { setText(""); go({ q: "" }); }} aria-label="Clear search"
            className="absolute right-1.5 top-1/2 -translate-y-1/2 grid place-items-center size-8 rounded-md text-ink-3 hover:bg-surface-2"><X size={16} aria-hidden /></button>
        )}
      </div>
      <div className="flex items-center gap-2">
        <div className="flex flex-wrap gap-1.5 flex-1" role="group" aria-label="Filter by status">
          {STATUS.map((s) => (
            <button key={s.value} type="button" onClick={() => go({ status: s.value })} aria-pressed={status === s.value}
              className={`h-8 rounded-full px-3 text-[13px] font-medium whitespace-nowrap border transition-colors ${
                status === s.value ? "bg-ink text-white border-ink" : "bg-surface text-ink-2 border-line-strong hover:bg-surface-2"}`}>
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <select value={intent} onChange={(e) => go({ intent: e.target.value })} aria-label="Filter by topic"
        className="w-full h-10 rounded-lg border border-line-strong bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/25">
        {INTENTS.map((i) => <option key={i.value} value={i.value}>{i.label}</option>)}
      </select>
    </div>
  );
}
