"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/client";
import type { Me } from "@/lib/types";

export default function LoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      const me = await api<Me>("/api/auth/login", "POST", { email, password });
      router.replace(me.stores[0] ? `/${me.stores[0].slug}` : "/");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign in.");
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-8 space-y-4" noValidate>
      <div>
        <label htmlFor="email" className="block text-sm font-medium mb-1.5">Email</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)}
          className="w-full h-11 rounded-lg border border-line-strong bg-surface px-3.5 outline-none focus:border-accent focus:ring-2 focus:ring-accent/25" />
      </div>
      <div>
        <label htmlFor="password" className="block text-sm font-medium mb-1.5">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)}
          className="w-full h-11 rounded-lg border border-line-strong bg-surface px-3.5 outline-none focus:border-accent focus:ring-2 focus:ring-accent/25" />
      </div>
      {error && <p role="alert" className="rounded-lg bg-bad-soft text-bad px-3.5 py-2.5 text-sm">{error}</p>}
      <button type="submit" disabled={pending || !email || !password}
        className="w-full h-11 rounded-lg bg-accent text-accent-ink font-medium whitespace-nowrap inline-flex items-center justify-center gap-2 hover:brightness-110 active:scale-[0.99] transition disabled:opacity-60">
        {pending && <Loader2 className="animate-spin" size={18} aria-hidden />}
        {pending ? "Signing in" : "Sign in"}
      </button>
    </form>
  );
}
