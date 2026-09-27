import { defineConfig, devices } from '@playwright/test';
import { PREVIEW_PORT, SITE_URL } from './tests/browser/routes';

/**
 * Browser gates for the prerendered site in dist/.
 *
 * The suite runs against `vite preview`, which honours the /sample-ai-agent-factory/
 * base path. A plain static server at / would serve a blank page that passes axe.
 * Run `npm run build` first; `npm run test:browser` then starts the preview server.
 * The site makes no external requests, so nothing here waits on the network.
 */
export default defineConfig({
  testDir: 'tests/browser',
  outputDir: 'test-results',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [['list'], ['github'], ['html', { open: 'never', outputFolder: 'playwright-report' }]]
    : [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: SITE_URL,
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  webServer: {
    command: `npm run preview -- --host 127.0.0.1 --port ${PREVIEW_PORT} --strictPort`,
    url: SITE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
  projects: [
    {
      name: 'mobile',
      // Rendered text does not depend on the viewport; content checks run once, on desktop.
      testIgnore: /content\.spec\.ts$/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } },
    },
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
});
