// Vitest configuration for the PULSE PWA (mirrors LUMI).
// Uses the React plugin for JSX/TSX, a jsdom environment for component tests,
// global test APIs, and the `@` path alias so tests resolve modules the same
// way the app does. Component/property tests are added in a later batch
// (Tasks 21.5-21.7); this config is in place so `npm run test` works.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // Shared StayOS auth module (single source of truth, resolved via the same
      // path alias the app uses in tsconfig/next.config).
      '@stayos/auth': path.resolve(__dirname, '../../shared/auth/src/index.ts'),
    },
  },
});
