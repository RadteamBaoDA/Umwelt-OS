import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirrors tsconfig.json compilerOptions.paths: "@/*" -> "./src/*".
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // Mirrors tsconfig paths: "@locales" -> JSON message catalogs.
      '@locales': fileURLToPath(new URL('./locales/index.ts', import.meta.url)),
      // Vitest resolves this test stub; Next retains its server-only import boundary in production.
      'server-only': fileURLToPath(new URL('./src/test/server-only.ts', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    // Windows: Node resolves externalized jest-dom's `import 'vitest'` with the cwd drive-letter case; a lower-case
    // `d:\` cwd then loads a second vitest/chai and every jest-dom matcher fails as "Invalid Chai property".
    // Inlining jest-dom routes that import through Vite so it shares the test files' vitest instance.
    server: { deps: { inline: ['@testing-library/jest-dom'] } },
    include: ['src/**/*.test.{ts,tsx}', 'locales/**/*.test.ts'],
    exclude: ['node_modules', '.next', '../../tests/**', '**/*.spec.{ts,tsx}'],
    // Coverage runs only via `npm run test:coverage` (CI). Floors in docs/development.md "Coverage floors".
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/test/**', 'src/**/*.d.ts'],
      reporter: ['text-summary', 'json-summary'],
      // Ratchet floors, set ~1.5 points under the measured 61.7/58.1/51.0/48.5 (lines/statements/functions/branches).
      thresholds: { lines: 60, statements: 57, functions: 50, branches: 47 },
    },
  },
});
