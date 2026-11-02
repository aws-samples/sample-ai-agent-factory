import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    // The API-client tests assert SAME-ORIGIN relative "/api/..." URLs. Pin the API base URL
    // empty for the test run (vitest test.env takes precedence over the ambient process env) so an
    // ambient VITE_API_BASE_URL -- e.g. the HTTPS fixture the release/CI suite exports for the
    // build and production-build-verify steps -- cannot turn those relative assertions absolute.
    // Build and verify read VITE_API_BASE_URL via vite.config.ts and are unaffected.
    env: { VITE_API_BASE_URL: '' },
  },
})
