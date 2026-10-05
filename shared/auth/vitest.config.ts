// Vitest config for the shared StayOS auth module.
//
// Uses the jsdom environment so the storage abstraction (localStorage) and JWT
// decoding (atob) run against a real DOM implementation, matching how the code
// executes in the browser. Mirrors the LUMI/PULSE frontend test setup.

import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
});
