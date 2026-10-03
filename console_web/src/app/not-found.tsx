import Link from "next/link";
import { SearchX } from "lucide-react";

export default function NotFound() {
  return (
    <main className="min-h-screen grid place-items-center px-6 text-center">
      <div>
        <div className="mx-auto grid place-items-center size-12 rounded-2xl bg-surface-2 text-ink-3 mb-4"><SearchX size={22} aria-hidden /></div>
        <h1 className="text-xl font-semibold">We could not find that</h1>
        <p className="mt-1 text-ink-2 max-w-sm mx-auto">The page does not exist, or you do not have access to it.</p>
        <Link href="/" className="mt-5 inline-flex h-10 items-center rounded-lg bg-ink px-4 text-sm font-medium text-white hover:opacity-90">Back to your stores</Link>
      </div>
    </main>
  );
}
