import { cookies } from "next/headers";
import { notFound, redirect } from "next/navigation";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8010";

/** Like apiGet, but a failure comes back as a message instead of throwing: for a panel that may fail on its own
 * (a store outage) without taking the whole page down. A 401 still sends the user to sign in. */
export async function apiTry<T>(path: string): Promise<{ data?: T; error?: string }> {
  const jar = await cookies();
  try {
    const res = await fetch(BACKEND + path, { headers: { cookie: jar.toString() }, cache: "no-store" });
    if (res.status === 401) redirect("/login");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { error: typeof body?.detail === "string" ? body.detail : `The service answered ${res.status}.` };
    }
    return { data: (await res.json()) as T };
  } catch (e) {
    if (e && typeof e === "object" && "digest" in e) throw e; // let redirect() through
    return { error: "The service could not be reached." };
  }
}

/** Server-side GET: forwards the browser's session cookie to the API. 401 sends the user to sign in, 404 to not-found. */
export async function apiGet<T>(path: string): Promise<T> {
  const jar = await cookies();
  const res = await fetch(BACKEND + path, { headers: { cookie: jar.toString() }, cache: "no-store" });
  if (res.status === 401) redirect("/login");
  if (res.status === 404) notFound();
  if (res.status === 403) redirect("/");
  if (!res.ok) throw new Error(`The service answered ${res.status} for ${path}`);
  return (await res.json()) as T;
}
