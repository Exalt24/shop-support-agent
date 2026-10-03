const MIN = 60_000;

export function ago(iso: string, now: number = Date.now()): string {
  const diff = now - new Date(iso).getTime();
  if (diff < MIN) return "just now";
  if (diff < 60 * MIN) return `${Math.floor(diff / MIN)} min ago`;
  if (diff < 24 * 60 * MIN) return `${Math.floor(diff / (60 * MIN))} h ago`;
  const d = Math.floor(diff / (24 * 60 * MIN));
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function money(amount: string | undefined, currency: string | undefined): string {
  if (!amount) return "";
  const n = Number(amount);
  return `${Number.isFinite(n) ? n.toFixed(2) : amount} ${currency ?? ""}`.trim();
}

export const INTENT_LABEL: Record<string, string> = {
  order_status: "Order status",
  return_request: "Return",
  policy_question: "Policy question",
  product_question: "Product question",
  other: "Other",
};

export const STATUS_LABEL: Record<string, string> = {
  open: "Open",
  answered: "Answered by agent",
  escalated: "Needs a person",
  resolved: "Resolved",
};

/** Plain-language names for the escalation reasons the engine emits, so a person never reads an internal code. */
export const REASON_LABEL: Record<string, string> = {
  legal: "Legal threat",
  "payment dispute": "Payment dispute",
  "asked for a person": "Asked for a person",
  safety: "Safety concern",
  "out of scope": "Outside what the agent handles",
  "reply failed the grounding check twice": "The agent could not answer without guessing",
  "store lookup failed": "The store did not respond",
  "triage failed": "The agent could not read the message",
  "answer generation failed": "The agent could not write a reply",
  "no policy section matched": "No policy covers this",
};

export function reasonText(reason: string | null | undefined): string {
  if (!reason) return "";
  if (REASON_LABEL[reason]) return REASON_LABEL[reason];
  if (reason.startsWith("model flagged:")) return `Flagged by the agent: ${reason.split(":")[1].trim()}`;
  return reason;
}

export function initials(email: string): string {
  const local = email.split("@")[0].replace(/[^a-zA-Z]/g, " ").trim();
  const parts = local.split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function nameFromEmail(email: string): string {
  const local = email.split("@")[0].replace(/[._-]+/g, " ").trim();
  return local.replace(/\b\w/g, (c) => c.toUpperCase());
}
