import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// First gate only: no session cookie means the login page. The cookie is signed and checked by the API on every
// call, so this is a convenience redirect, not the security boundary.
export function proxy(request: NextRequest) {
  const hasSession = request.cookies.has("console_session");
  const { pathname } = request.nextUrl;
  if (!hasSession && pathname !== "/login") {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  if (hasSession && pathname === "/login") {
    return NextResponse.redirect(new URL("/", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|.*\\.(?:png|svg|jpg|ico|webmanifest)$).*)"],
};
