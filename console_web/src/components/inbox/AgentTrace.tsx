"use client";

import { useState } from "react";
import {
  AlertTriangle, BookOpen, ChevronDown, CircleHelp, Hand, ListChecks, Package, ShieldAlert, ShieldCheck, ShoppingBag,
} from "lucide-react";
import type { Meta } from "@/lib/types";

type Tone = "neutral" | "ok" | "warn";
interface Step { icon: typeof Package; text: string; tone: Tone }

/** Turns the engine's step log into a sentence a support person can read. The raw step stays available as a tooltip. */
export function explain(step: string): Step {
  const s = step.toLowerCase();
  if (s.startsWith("hard escalation")) return { icon: Hand, text: "Matched a rule that always goes to a person, before reading anything else", tone: "warn" };
  if (s.startsWith("triage:")) return { icon: ListChecks, text: `Read the message: ${step.split("intent=")[1]?.replace(/_/g, " ") ?? "classified"}`, tone: "neutral" };
  if (s.startsWith("order lookup: found")) return { icon: Package, text: `Looked up ${step.split("found ")[1]} and the email matched`, tone: "neutral" };
  if (s.startsWith("order lookup: none")) return { icon: Package, text: "No order for this email (same answer as a number that does not exist)", tone: "neutral" };
  if (s.startsWith("policy:")) return { icon: BookOpen, text: `Read policy: ${step.slice(7).trim()}`, tone: "neutral" };
  if (s.startsWith("product search")) return { icon: ShoppingBag, text: `Searched the catalogue: ${step.split(": ")[1] ?? ""}`, tone: "neutral" };
  if (s.startsWith("guard: passed")) return { icon: ShieldCheck, text: "Checked every figure and promise against the facts: passed", tone: "ok" };
  if (s.startsWith("guard: refused")) return { icon: ShieldAlert, text: `Blocked a draft reply: ${step.replace(/^guard: refused attempt \d+ \(/, "").replace(/\)$/, "")}`, tone: "warn" };
  if (s.startsWith("escalated")) return { icon: Hand, text: `Handed to a person: ${step.replace("escalated: ", "")}`, tone: "warn" };
  if (s.startsWith("asked for the order number")) return { icon: CircleHelp, text: "Asked the customer for their order number", tone: "neutral" };
  if (s.includes("error") || s.includes("failed")) return { icon: AlertTriangle, text: step, tone: "warn" };
  return { icon: ListChecks, text: step, tone: "neutral" };
}

const TONE: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-2 border-line",
  ok: "bg-ok-soft text-ok border-transparent",
  warn: "bg-warn-soft text-warn border-transparent",
};

/** A disclosure (button + aria-expanded), not a listbox: this is a toggle for supporting detail. */
export default function AgentTrace({ meta, defaultOpen = false, label = "What the agent did" }: { meta: Meta; defaultOpen?: boolean; label?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  const steps = meta.steps ?? [];
  if (!steps.length) return null;
  return (
    <div className="mt-2">
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1.5 text-xs font-medium text-ink-3 hover:text-ink rounded-md py-1">
        <ChevronDown size={14} className={`transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
        {label} <span className="text-ink-3 font-normal">· {steps.length} steps</span>
      </button>
      {open && (
        <ol className="mt-1.5 space-y-1.5">
          {steps.map((raw, i) => {
            const { icon: Icon, text, tone } = explain(raw);
            return (
              <li key={i} title={raw} className={`flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-xs leading-snug ${TONE[tone]}`}>
                <Icon size={14} className="mt-px shrink-0" aria-hidden />
                <span>{text}</span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
