import type { Metadata } from "next";
import ChatConsole from "@/components/ChatConsole";
import { PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Live chat" };

export default async function ChatPage({ params }: PageProps<"/[store]/chat">) {
  const { store } = await params;
  return (
    <div className="p-4 sm:p-6 lg:p-8 max-w-5xl">
      <PageHeader title="Live chat" subtitle="Try the agent the way a customer would. Answers come from the store's live orders and its policy." />
      <ChatConsole store={store} />
    </div>
  );
}
