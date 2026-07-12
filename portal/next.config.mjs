/**
 * Next.js configuration.
 *
 * `output: "standalone"` produces a self-contained server bundle (server.js +
 * a pruned node_modules) so the container image stays small and needs no npm
 * install at runtime - important for the airgapped mirror. See the Dockerfile.
 */

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // The portal is only ever reached through its own Route/ingress; it never
  // trusts an inbound `x-powered-by` disclosure.
  poweredByHeader: false,
};

export default nextConfig;
