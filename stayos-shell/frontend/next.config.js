// Next.js configuration for the StayOS shell PWA.
//
// The shell owns the site root ("/") on the shared StayOS CloudFront
// distribution: it presents the login form when unauthenticated and the feature
// grid (launcher into LUMI at /lumi and PULSE at /pulse) when authenticated.
// Mirrors the LUMI/PULSE stack: static HTML export (output: 'export') for S3 +
// CloudFront hosting behind the shared WAF, unoptimized images (no server
// runtime), and trailing slashes for stable static routes. basePath is left
// UNSET so the shell is served from "/".
const path = require('path');

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
  // Transpile the shared StayOS auth module (TS source outside this app root)
  // through Next's build pipeline so it is bundled into the static export.
  transpilePackages: ['@stayos/auth'],
  webpack: (config) => {
    // Resolve the @stayos/auth path alias to the shared module source. Mirrors
    // the tsconfig `paths` and vitest alias so build/typecheck/test agree.
    config.resolve.alias['@stayos/auth'] = path.resolve(
      __dirname,
      '../../shared/auth/src/index.ts',
    );
    return config;
  },
};

module.exports = nextConfig;
