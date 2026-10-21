import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirrors tsconfig.json compilerOptions.paths: "@/*" -> "./src/*".
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // Vitest resolves this test stub; Next retains its server-only import boundary in production.
      'server-only': fileURLToPath(new URL('./src/test/server-only.ts', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['node_modules', '.next', '../../tests/**', '**/*.spec.{ts,tsx}'],
  },
});
