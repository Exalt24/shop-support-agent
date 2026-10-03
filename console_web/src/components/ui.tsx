import { CheckCircle2, CheckCheck, Circle, Hand } from "lucide-react";
import type { ReactNode } from "react";
import type { Status } from "@/lib/types";
import { STATUS_LABEL } from "@/lib/format";

const STATUS_STYLE: Record<Status, { cls: string; Icon: typeof Circle }> = {
  open: { cls: "bg-surface-2 text-ink-2", Icon: Circle },
  answered: { cls: "bg-ok-soft text-ok", Icon: CheckCircle2 },
  escalated: { cls: "bg-warn-soft text-warn", Icon: Hand },
  resolved: { cls: "bg-done-soft text-done", Icon: CheckCheck },
};

/** Colour is never the only signal: every status carries an icon and a word. */
export function StatusChip({ status, compact = false }: { status: Status; compact?: boolean }) {
  const { cls, Icon } = STATUS_STYLE[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap ${cls}`}>
      <Icon size={13} aria-hidden />
      {compact ? STATUS_LABEL[status].replace(" by agent", "") : STATUS_LABEL[status]}
    </span>
  );
}

export function Chip({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "accent" | "warn" | "ok" }) {
  const t = {
    neutral: "bg-surface-2 text-ink-2",
    accent: "bg-accent-soft text-accent",
    warn: "bg-warn-soft text-warn",
    ok: "bg-ok-soft text-ok",
  }[tone];
  return <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ${t}`}>{children}</span>;
}

export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-3 mb-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-ink-2">{subtitle}</p>}
      </div>
      {children}
    </header>
  );
}

export function EmptyState({ icon, title, body, action }: { icon: ReactNode; title: string; body: string; action?: ReactNode }) {
  return (
    <div className="grid place-items-center text-center py-16 px-6">
      <div className="grid place-items-center size-12 rounded-2xl bg-surface-2 text-ink-3 mb-4">{icon}</div>
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-1 text-ink-2 max-w-sm">{body}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`bg-surface border border-line rounded-[var(--radius)] shadow-[var(--shadow)] ${className}`}>{children}</div>;
}

export function Avatar({ label, tone = "neutral" }: { label: string; tone?: "neutral" | "accent" }) {
  return (
    <span className={`grid place-items-center size-8 shrink-0 rounded-full text-xs font-semibold ${tone === "accent" ? "bg-accent text-white" : "bg-surface-2 text-ink-2"}`}>
      {label}
    </span>
  );
}
