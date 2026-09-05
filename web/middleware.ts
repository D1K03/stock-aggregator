import { NextResponse, type NextRequest } from "next/server";

/* A presence check, not a validity check.
 *
 * The session cookie is opaque and only its HMAC is stored, so nothing here can
 * tell a real session from a forged string; the status service holds the secret
 * and the table and is the only authority on that. What this buys is that a
 * signed-out visitor lands on /login instead of a dashboard that would then
 * fail every request behind it.
 *
 * A forged cookie gets you the shell and nothing in it. */
const SESSION_COOKIE = "screener_session";

export function middleware(request: NextRequest) {
  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();

  const url = request.nextUrl.clone();
  url.pathname = "/login";
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except the login page itself, Next's own assets, and the paths
  // Caddy hands to the status service.
  matcher: ["/((?!login|_next/static|_next/image|favicon.ico|auth|api|health|ready|status).*)"],
};
