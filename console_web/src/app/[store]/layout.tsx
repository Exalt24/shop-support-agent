import { notFound } from "next/navigation";
import Shell from "@/components/Shell";
import { apiGet } from "@/lib/api";
import type { Me } from "@/lib/types";

export default async function StoreLayout({ children, params }: LayoutProps<"/[store]">) {
  const { store: slug } = await params;
  const me = await apiGet<Me>("/api/me");
  const store = me.stores.find((s) => s.slug === slug);
  if (!store) notFound();
  return (
    <Shell store={store} stores={me.stores} user={me.user}>
      {children}
    </Shell>
  );
}
