import type { Metadata } from "next";
import LookupPanel from "@/components/LookupPanel";
import { PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Lookup" };

export default async function LookupPage({ params }: PageProps<"/[store]/lookup">) {
  const { store } = await params;
  return (
    <div className="p-4 sm:p-6 lg:p-8">
      <PageHeader title="Lookup" subtitle="Check an order or a product without leaving the console." />
      <LookupPanel store={store} />
    </div>
  );
}
