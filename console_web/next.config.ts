import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api is proxied to the FastAPI service, so the session cookie is
// first-party and no CORS is needed. Server components call the backend directly (see src/lib/api.ts).
const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8010";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
};

export default nextConfig;
