"use client";

import { useState } from "react";
import { Loader2, Package, Search, ShoppingBag } from "lucide-react";
import { api } from "@/lib/client";
import { money } from "@/lib/format";
import type { OrderView, ProductView } from "@/lib/types";
import { Card, Chip } from "@/components/ui";
import OrderCard from "@/components/OrderCard";

type Mode = "order" | "product";

export default function LookupPanel({ store }: { store: string }) {
  const [mode, setMode] = useState<Mode>("order");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [order, setOrder] = useState<OrderView | null>(null);
  const [products, setProducts] = useState<ProductView[] | null>(null);

  async function go(e: React.FormEvent) {
    e.preventDefault();
    const term = q.trim();
    if (!term) return;
    setBusy(true);
    setError(null);
    setOrder(null);
    setProducts(null);
    try {
      if (mode === "order") setOrder(await api<OrderView>(`/api/t/${store}/orders/${encodeURIComponent(term.replace(/^#/, ""))}`));
      else setProducts((await api<{ items: ProductView[] }>(`/api/t/${store}/products?q=${encodeURIComponent(term)}`)).items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-2xl">
      <div role="tablist" aria-label="What to look up" className="flex gap-1 p-1 rounded-xl bg-surface-2 w-fit mb-4">
        {([["order", "Order", Package], ["product", "Product", ShoppingBag]] as const).map(([k, label, Icon]) => (
          <button key={k} role="tab" type="button" aria-selected={mode === k}
            onClick={() => { setMode(k); setOrder(null); setProducts(null); setError(null); setQ(""); }}
            className={`h-9 px-4 rounded-lg text-sm font-medium inline-flex items-center gap-2 ${mode === k ? "bg-surface shadow-sm text-ink" : "text-ink-2 hover:text-ink"}`}>
            <Icon size={15} aria-hidden />{label}
          </button>
        ))}
      </div>

      <form onSubmit={go} className="flex gap-2">
        <label htmlFor="q" className="sr-only">{mode === "order" ? "Order number" : "Product name"}</label>
        <input id="q" value={q} onChange={(e) => setQ(e.target.value)} inputMode={mode === "order" ? "numeric" : "text"}
          placeholder={mode === "order" ? "Order number, like 1019" : "Product name, like headphones"}
          className="flex-1 min-w-0 h-11 rounded-lg border border-line-strong bg-surface px-3.5 outline-none focus:border-accent focus:ring-2 focus:ring-accent/25" />
        <button type="submit" disabled={busy || q.trim().length < (mode === "order" ? 1 : 2)}
          className="h-11 rounded-lg bg-accent text-white px-4 font-medium inline-flex items-center gap-2 whitespace-nowrap hover:brightness-110 disabled:opacity-60">
          {busy ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <Search size={16} aria-hidden />}Look up
        </button>
      </form>
      <p className="mt-2 text-sm text-ink-3">Read live from the store. Each order lookup is recorded in the audit log.</p>

      <div className="mt-5" aria-live="polite">
        {error && <p role="alert" className="rounded-lg bg-bad-soft text-bad px-3.5 py-2.5 text-sm">{error}</p>}
        {order && <OrderCard order={order} />}
        {products && (products.length === 0 ? (
          <p className="rounded-lg bg-surface-2 px-3.5 py-2.5 text-sm text-ink-2">No active product matches that.</p>
        ) : (
          <ul className="space-y-3">
            {products.map((p) => (
              <li key={p.title}>
                <Card className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="font-semibold">{p.title}</div>
                    <div className="font-semibold whitespace-nowrap">{money(p.price, p.currency)}</div>
                  </div>
                  <div className="mt-1.5"><Chip tone={p.in_stock ? "ok" : "warn"}>{p.in_stock ? "In stock" : "Out of stock"}</Chip></div>
                  {p.description && <p className="mt-2 text-sm text-ink-2">{p.description}</p>}
                </Card>
              </li>
            ))}
          </ul>
        ))}
      </div>
    </div>
  );
}
