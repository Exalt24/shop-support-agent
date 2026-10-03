import type { Metadata } from "next";
import { CheckCircle2, KeyRound, Store } from "lucide-react";
import { apiGet } from "@/lib/api";
import PolicyEditor from "@/components/PolicyEditor";
import { Card, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Settings" };

interface Settings { name: string; shop_domain: string; token_connected: boolean; policy_md: string }

export default async function SettingsPage({ params }: PageProps<"/[store]/settings">) {
  const { store } = await params;
  const s = await apiGet<Settings>(`/api/t/${store}/settings`);
  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-3xl">
      <PageHeader title="Settings" subtitle={`How the agent works for ${s.name}.`} />

      <Card className="p-5">
        <h2 className="font-semibold">Store connection</h2>
        <dl className="mt-3 space-y-2.5 text-sm">
          <div className="flex items-center gap-3"><Store size={16} className="text-ink-3" aria-hidden /><dt className="w-28 text-ink-3">Shop</dt><dd className="font-mono text-[13px] break-all">{s.shop_domain}</dd></div>
          <div className="flex items-center gap-3"><KeyRound size={16} className="text-ink-3" aria-hidden /><dt className="w-28 text-ink-3">Access</dt>
            <dd className="inline-flex items-center gap-1.5 text-ok"><CheckCircle2 size={15} aria-hidden />{s.token_connected ? "Connected, read-only. The key is stored encrypted and never shown." : "Not connected"}</dd></div>
        </dl>
      </Card>

      <Card className="p-5 mt-4">
        <h2 className="font-semibold">Store policy</h2>
        <p className="text-sm text-ink-2 mt-0.5 mb-4">Returns, shipping, warranty. Edit it and the agent changes what it says, with no deploy.</p>
        <PolicyEditor store={store} initial={s.policy_md} />
      </Card>
    </div>
  );
}
