export type Role = "admin" | "agent";
export type Status = "open" | "answered" | "escalated" | "resolved";

export interface Store { slug: string; name: string; role: Role }
export interface Me { user: { id: string; email: string; name: string }; stores: Store[] }

export interface ConversationRow {
  id: string; customer_email: string; status: Status; intent: string | null; order_name: string | null;
  updated_at: string; channel: string; assignee: string | null; preview: string | null;
}
export interface Meta {
  intent?: string; action?: string; order?: string | null; steps?: string[]; guard_violations?: string[];
  escalation_reason?: string | null;
}
export interface Message {
  id: string; sender: "customer" | "agent" | "human"; body: string; meta: Meta; created_at: string; author: string | null;
}
export interface Handoff {
  id: string; reason: string; status: "open" | "claimed" | "resolved"; resolution_note: string | null;
  created_at: string; resolved_at: string | null; claimed_by_name: string | null; claimed_by: string | null;
}
export interface ConversationDetail {
  conversation: ConversationRow & { assigned_to: string | null; created_at: string };
  messages: Message[];
  handoff: Handoff | null;
}
export interface HandoffRow {
  id: string; conversation_id: string; reason: string; status: "open" | "claimed" | "resolved"; created_at: string;
  resolved_at: string | null; customer_email: string; intent: string | null; order_name: string | null;
  claimed_by_name: string | null; claimed_by: string | null; preview: string | null;
}
export interface Dashboard {
  store: string; role: Role;
  week: { total: number; answered: number; escalated: number; resolved: number };
  handoffs: { open: number; claimed: number };
  days: { day: string; n: number }[];
  recent: { id: string; customer_email: string; status: Status; intent: string | null; order_name: string | null; updated_at: string }[];
  agent_handled_pct: number | null;
}
export interface AuditEntry { id: number; actor: string; action: string; detail: Record<string, unknown>; created_at: string }
export interface OrderView {
  found: boolean; name?: string; email?: string; placed?: string; payment?: string; fulfillment?: string;
  total?: string; currency?: string; items?: string[]; tracking?: string[];
}
export interface ProductView { title: string; price: string; currency: string; in_stock: boolean; description: string }
export interface ChatReply {
  conversation_id: string; reply: string; action: "replied" | "escalated" | "held"; escalation_reason: string | null;
  intent: string | null; steps: string[];
}
