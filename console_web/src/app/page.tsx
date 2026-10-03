import { redirect } from "next/navigation";
import { apiGet } from "@/lib/api";
import type { Me } from "@/lib/types";

// "/" sends you to your first store. A person with no stores gets a plain explanation instead of a blank page.
export default async function Home() {
  const me = await apiGet<Me>("/api/me");
  if (me.stores.length) redirect(`/${me.stores[0].slug}`);
  return (
    <main className="min-h-screen grid place-items-center p-8">
      <div className="max-w-sm text-center">
        <h1 className="text-xl font-semibold">No stores yet</h1>
        <p className="mt-2 text-ink-2">Your account is not a member of any store. Ask an admin to add you.</p>
      </div>
    </main>
  );
}
