import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { test } from '@playwright/test';
import { DIST_DIR, href } from './routes';

/**
 * Full-page captures for review, not a gate. Skipped unless SCREENSHOTS=1.
 *
 *   SCREENSHOTS=1 SHOT_DIR=/tmp/shots npx playwright test screenshots
 *
 * Runs in every configured project (light and dark, desktop and mobile). Animations
 * are disabled and reduced motion requested so captures are deterministic. Files land
 * outside dist/ (default `screenshots/`, git-ignored).
 */
const ROUTES = [
  '/',
  '/start/',
  '/start/which-project/',
  '/start/faq/',
  '/concepts/agent-factory/',
  '/concepts/capability-contracts/',
  '/concepts/architecture/',
  '/concepts/glossary/',
  '/projects/',
  '/projects/blueprint/',
  '/projects/workshop/',
  '/projects/mcp-gateway/policies/',
  '/reference/security/',
  '/reference/support-envelope/',
  '/projects/blueprint/readme/',
  '/contributing/',
];

test.describe('screenshots', () => {
  test.skip(!process.env.SCREENSHOTS, 'set SCREENSHOTS=1 to capture');

  for (const route of ROUTES) {
    test(`capture ${route}`, async ({ page }, testInfo) => {
      const dir = process.env.SHOT_DIR ?? join(DIST_DIR, '..', 'screenshots');
      mkdirSync(dir, { recursive: true });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.goto(href(route), { waitUntil: 'networkidle' });
      await page.addStyleTag({ content: 'html { scroll-behavior: auto !important; }' });
      // Lazy images load once scrolled into view.
      await page.evaluate(async () => {
        for (let y = 0; y <= document.documentElement.scrollHeight; y += 700) {
          window.scrollTo(0, y);
          await new Promise((resolve) => setTimeout(resolve, 60));
        }
        window.scrollTo(0, 0);
      });
      await page.waitForTimeout(300);
      const name = `${testInfo.project.name}${route.replace(/\//g, '_') || '_'}.png`;
      await page.screenshot({ path: join(dir, name), fullPage: true, animations: 'disabled' });
    });
  }
});
