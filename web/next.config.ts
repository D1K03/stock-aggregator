import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits .next/standalone: a self-contained server plus only the node_modules
  // it actually reaches. The runtime image then carries no build toolchain and
  // no dev dependencies.
  output: "standalone",
};

export default nextConfig;
