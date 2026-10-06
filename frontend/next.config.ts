import path from "node:path";
import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";
// Pin the project root so a lockfile in a parent directory never changes tracing or the standalone layout.
const projectRoot = path.resolve(__dirname);

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  outputFileTracingRoot: projectRoot,
  turbopack: { root: projectRoot },
  experimental: {
    // The /api rewrite proxy times out after 30 s by default (next/dist/server/lib/router-utils/proxy-request.js);
    // a chat turn may run up to TURN_DEADLINE_SECONDS (45 s), so the proxy must wait longer than that.
    proxyTimeout: 120_000,
  },
  async redirects() {
    return [{ source: "/", destination: "/chat", permanent: false }];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;
