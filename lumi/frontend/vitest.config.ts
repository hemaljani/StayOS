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
