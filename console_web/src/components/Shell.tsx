"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ChevronsUpDown, Hand, Inbox, LayoutDashboard, LogOut, Menu, MessagesSquare, MessageCircle, ScrollText, Search,
  Settings, X,
} from "lucide-react";
import { api } from "@/lib/client";
import type { Dashboard, Role, Store } from "@/lib/types";

interface Props {
  store: Store;
  stores: Store[];
  user: { name: string; email: string };
  children: React.ReactNode;
}

function initials(name: string) {
  return name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
}

export default function Shell({ store, stores, user, children }: Props) {
  const pathname = usePathname();
  const router = useRouter();
  const [drawer, setDrawer] = useState(false);
  const [switcher, setSwitcher] = useState(false);
  const [openHandoffs, setOpenHandoffs] = useState<number | null>(null);

  // the handoff count is the one number a person should never have to refresh for
  useEffect(() => {
    let alive = true;
    const load = () =>
      api<Dashboard>(`/api/t/${store.slug}/dashboard`)
        .then((d) => alive && setOpenHandoffs(d.handoffs.open + d.handoffs.claimed))
        .catch(() => {});
    load();
    const t = setInterval(load, 20000);
    return () => { alive = false; clearInterval(t); };
  }, [store.slug, pathname]);

  useEffect(() => setDrawer(false), [pathname]);

  const base = `/${store.slug}`;
  const items: { href: string; label: string; icon: typeof Inbox; admin?: boolean; badge?: number | null }[] = [
    { href: base, label: "Dashboard", icon: LayoutDashboard },
    { href: `${base}/inbox`, label: "Inbox", icon: Inbox },
    { href: `${base}/handoffs`, label: "Handoffs", icon: Hand, badge: openHandoffs },
    { href: `${base}/chat`, label: "Live chat", icon: MessageCircle },
    { href: `${base}/lookup`, label: "Lookup", icon: Search },
    { href: `${base}/audit`, label: "Audit log", icon: ScrollText, admin: true },
    { href: `${base}/settings`, label: "Settings", icon: Settings, admin: true },
  ];
  const visible = items.filter((i) => !i.admin || store.role === "admin");
  const active = (href: string) => (href === base ? pathname === base : pathname.startsWith(href));

  async function signOut() {
    await api("/api/auth/logout", "POST");
    router.replace("/login");
    router.refresh();
  }

  const nav = (
    <>
      <div className="relative px-3 pt-4">
        <button
          type="button" aria-expanded={switcher} aria-controls="store-menu" onClick={() => setSwitcher((s) => !s)}
          className="w-full flex items-center gap-3 rounded-xl px-2.5 py-2 hover:bg-surface-2 text-left"
        >
          <span className="grid place-items-center size-9 rounded-lg bg-accent text-white shrink-0"><MessagesSquare size={18} aria-hidden /></span>
          <span className="min-w-0 flex-1">
            <span className="block truncate font-semibold leading-tight">{store.name}</span>
            <span className="block text-xs text-ink-3 capitalize">{store.role}</span>
          </span>
          {stores.length > 1 && <ChevronsUpDown size={16} className="text-ink-3" aria-hidden />}
        </button>
        {switcher && stores.length > 1 && (
          <ul id="store-menu" className="absolute left-3 right-3 top-[calc(100%-4px)] z-30 rounded-xl border border-line bg-surface shadow-lg p-1.5">
            {stores.map((s) => (
              <li key={s.slug}>
                <Link href={`/${s.slug}`} onClick={() => setSwitcher(false)}
                  className={`flex items-center justify-between rounded-lg px-3 py-2 text-sm hover:bg-surface-2 ${s.slug === store.slug ? "font-semibold" : ""}`}>
                  {s.name}<span className="text-xs text-ink-3 capitalize">{s.role}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      <nav aria-label="Main" className="mt-4 px-3 flex-1 space-y-0.5">
        {visible.map(({ href, label, icon: Icon, badge }) => (
          <Link key={href} href={href} aria-current={active(href) ? "page" : undefined}
            className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-[15px] font-medium transition-colors ${
              active(href) ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-2 hover:text-ink"}`}>
            <Icon size={18} aria-hidden />
            <span className="flex-1">{label}</span>
            {badge ? (
              <span className="min-w-6 text-center rounded-full bg-warn-soft text-warn text-xs font-semibold px-1.5 py-0.5" aria-label={`${badge} open`}>{badge}</span>
            ) : null}
          </Link>
        ))}
      </nav>
      <div className="p-3 border-t border-line">
        <div className="flex items-center gap-3 rounded-xl px-2.5 py-2">
          <span className="grid place-items-center size-9 rounded-full bg-surface-2 text-sm font-semibold text-ink-2">{initials(user.name)}</span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{user.name}</span>
            <span className="block truncate text-xs text-ink-3">{user.email}</span>
          </span>
          <button type="button" onClick={signOut} aria-label="Sign out" title="Sign out"
            className="grid place-items-center size-9 rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink">
            <LogOut size={18} aria-hidden />
          </button>
        </div>
      </div>
    </>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[264px_minmax(0,1fr)]">
      <aside className="hidden lg:flex flex-col h-screen sticky top-0 bg-surface border-r border-line">{nav}</aside>

      <div className="lg:hidden sticky top-0 z-20 flex items-center justify-between h-14 px-4 bg-surface border-b border-line">
        <button type="button" onClick={() => setDrawer(true)} aria-label="Open menu" className="grid place-items-center size-10 -ml-2 rounded-lg hover:bg-surface-2">
          <Menu size={22} aria-hidden />
        </button>
        <span className="font-semibold truncate">{store.name}</span>
        <Link href={`${base}/handoffs`} aria-label={`${openHandoffs ?? 0} open handoffs`} className="relative grid place-items-center size-10 -mr-2 rounded-lg hover:bg-surface-2">
          <Hand size={20} aria-hidden />
          {openHandoffs ? <span className="absolute top-1 right-0.5 min-w-5 text-center rounded-full bg-warn-soft text-warn text-[11px] font-semibold px-1">{openHandoffs}</span> : null}
        </Link>
      </div>
      {drawer && (
        <div className="lg:hidden fixed inset-0 z-40 flex">
          <div className="absolute inset-0 bg-black/30" onClick={() => setDrawer(false)} aria-hidden />
          <aside className="relative flex flex-col w-72 max-w-[85%] bg-surface shadow-xl">
            <button type="button" onClick={() => setDrawer(false)} aria-label="Close menu" className="absolute top-3 right-3 grid place-items-center size-9 rounded-lg hover:bg-surface-2"><X size={20} aria-hidden /></button>
            {nav}
          </aside>
        </div>
      )}

      <main className="min-w-0">{children}</main>
    </div>
  );
}

export type { Role };
