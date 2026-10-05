// Vitest configuration for the StayOS shell PWA (mirrors LUMI/PULSE).
// React plugin for JSX/TSX, jsdom environment for component tests, global test
// APIs, and the `@` + `@stayos/auth` path aliases so tests resolve modules the
// same way the app does.
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
      // Shared StayOS auth module (single source of truth).
      '@stayos/auth': path.resolve(__dirname, '../../shared/auth/src/index.ts'),
    },
  },
});
