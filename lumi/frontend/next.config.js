/** @type {import('next').NextConfig} */
const path = require('path');

const nextConfig = {
  output: 'export',
  // LUMI is served from /lumi/ on the shared StayOS CloudFront distribution; the
  // StayOS shell owns the site root ("/"). Every route and asset is prefixed with
  // /lumi. next/link, next/router, and next/image apply this automatically; raw
  // window.location/serviceWorker/manifest/worklet paths are NOT, so those use the
  // BASE_PATH constant / withBase() in src/lib/constants.ts.
  basePath: '/lumi',
  assetPrefix: '/lumi',
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
