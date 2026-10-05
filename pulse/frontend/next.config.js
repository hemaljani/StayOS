// Next.js configuration for the PULSE PWA.
// Mirrors LUMI: static HTML export (`output: 'export'`) so the app can be
// hosted on S3 + CloudFront behind the shared StayOS WAF, with unoptimized
// images (no server runtime) and trailing slashes for stable static routes.
//
// basePath/assetPrefix: PULSE is served from the /pulse/ path on the SHARED
// StayOS (LUMI) CloudFront distribution + S3 bucket (Option A), so every route
// and asset is prefixed with /pulse. next/link and router.push are prefixed
// automatically; raw window.location/serviceWorker/manifest paths are NOT, so
// those use the BASE_PATH constant in src/lib/constants.ts.
const path = require('path');

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  basePath: '/pulse',
  assetPrefix: '/pulse',
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
