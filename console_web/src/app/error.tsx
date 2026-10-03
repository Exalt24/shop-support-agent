"use client";

import { TriangleAlert } from "lucide-react";

export default function ErrorPage({ unstable_retry }: { error: Error & { digest?: string }; unstable_retry: () => void }) {
  return (
    <main className="min-h-[60vh] grid place-items-center px-6 text-center">
      <div>
        <div className="mx-auto grid place-items-center size-12 rounded-2xl bg-warn-soft text-warn mb-4"><TriangleAlert size={22} aria-hidden /></div>
        <h1 className="text-xl font-semibold">That did not load</h1>
        <p className="mt-1 text-ink-2 max-w-sm mx-auto">The service may be busy. Nothing was lost. Try again in a moment.</p>
        <button type="button" onClick={() => unstable_retry()} className="mt-5 h-10 rounded-lg bg-ink px-4 text-sm font-medium text-white hover:opacity-90">Try again</button>
      </div>
    </main>
  );
}
