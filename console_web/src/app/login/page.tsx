import type { Metadata } from "next";
import { MessagesSquare, ShieldCheck, Hand } from "lucide-react";
import LoginForm from "./LoginForm";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <main className="min-h-screen grid lg:grid-cols-[1.05fr_1fr]">
      <section className="hidden lg:flex flex-col justify-between bg-[#1f1d3a] text-white p-12">
        <div className="flex items-center gap-2.5 text-lg font-semibold">
          <span className="grid place-items-center size-9 rounded-xl bg-white/10"><MessagesSquare size={20} aria-hidden /></span>
          Support console
        </div>
        <div className="max-w-md">
          <h1 className="text-4xl font-semibold leading-tight tracking-tight">An agent that answers from your orders, and knows when to stop.</h1>
          <ul className="mt-8 space-y-4 text-[15px] text-white/80">
            <li className="flex gap-3"><ShieldCheck className="mt-0.5 shrink-0" size={20} aria-hidden />Every figure in a reply is checked against your store before it is sent.</li>
            <li className="flex gap-3"><Hand className="mt-0.5 shrink-0" size={20} aria-hidden />Legal threats, disputes and refund demands go straight to your team, with everything the agent did.</li>
          </ul>
        </div>
        <p className="text-sm text-white/60">Connected stores keep separate orders, policies and conversations.</p>
      </section>
      <section className="flex items-center justify-center p-6 sm:p-12">
        <div className="w-full max-w-sm">
          <div className="lg:hidden flex items-center gap-2 text-lg font-semibold mb-8">
            <span className="grid place-items-center size-9 rounded-xl bg-accent text-white"><MessagesSquare size={20} aria-hidden /></span>
            Support console
          </div>
          <h2 className="text-2xl font-semibold tracking-tight">Sign in</h2>
          <p className="mt-1.5 text-ink-2">Use the account your admin set up for you.</p>
          <LoginForm />
        </div>
      </section>
    </main>
  );
}
