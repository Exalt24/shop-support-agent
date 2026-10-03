import { CheckCircle2, Clock3, Package, Truck } from "lucide-react";
import { clock, money } from "@/lib/format";
import type { OrderView } from "@/lib/types";

function Pill({ ok, icon, children }: { ok: boolean; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${ok ? "bg-ok-soft text-ok" : "bg-warn-soft text-warn"}`}>
      {icon}{children}
    </span>
  );
}

/** A staff view of one order. Status always has an icon and a word, never colour alone. */
export default function OrderCard({ order }: { order: OrderView }) {
  if (!order.found) {
    return <p className="rounded-lg bg-surface-2 px-3 py-2.5 text-sm text-ink-2">No order with that number in this store.</p>;
  }
  const shipped = order.fulfillment === "fulfilled";
  return (
    <div className="rounded-xl border border-line bg-surface p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-semibold text-[15px]">{order.name}</div>
          <div className="text-xs text-ink-3">{order.placed ? `Placed ${clock(order.placed)}` : ""}</div>
        </div>
        <div className="text-right font-semibold">{money(order.total, order.currency)}</div>
      </div>
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <Pill ok={order.payment === "paid"} icon={order.payment === "paid" ? <CheckCircle2 size={13} aria-hidden /> : <Clock3 size={13} aria-hidden />}>
          {order.payment === "paid" ? "Paid" : `Payment: ${order.payment}`}
        </Pill>
        <Pill ok={shipped} icon={shipped ? <Truck size={13} aria-hidden /> : <Package size={13} aria-hidden />}>
          {shipped ? "Shipped" : "Not shipped yet"}
        </Pill>
      </div>
      {order.items && order.items.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-sm text-ink-2">
          {order.items.map((i) => <li key={i}>{i}</li>)}
        </ul>
      )}
      <div className="mt-3 text-sm">
        <span className="text-ink-3">Tracking </span>
        {order.tracking && order.tracking.length ? (
          <span className="font-mono text-[13px]">{order.tracking.join(", ")}</span>
        ) : (
          <span className="text-ink-2">none yet</span>
        )}
      </div>
      {order.email && <div className="mt-1 text-sm text-ink-3 truncate">{order.email}</div>}
    </div>
  );
}
