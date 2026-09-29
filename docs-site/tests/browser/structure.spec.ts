import { expect, test } from '@playwright/test';
import { href, sweepRoutes } from './routes';

/**
 * Structural guardrails for the design system: every page has one dark page header
 * with the single h1, colour is never the only cue in the posture matrices, the Home
 * capability stack carries a text alternative for its dots, and doc pages carry the
 * progress bar. Runs in the light and dark projects.
 */
const { routes } = sweepRoutes();

test.describe('page header', () => {
  for (const route of routes) {
    test(`one dark page header with the single h1 on ${route}`, async ({ page }) => {
      await page.goto(href(route));
      await expect(page.locator('header.on-dark[data-page-header]')).toHaveCount(1);
      await expect(page.locator('header.on-dark[data-page-header] h1')).toHaveCount(1);
      await expect(page.locator('h1')).toHaveCount(1);
    });
  }
});

test.describe('theme', () => {
  test('the page follows the colour scheme of the project', async ({ page }, testInfo) => {
    await page.goto(href('/'));
    const dark = testInfo.project.use.colorScheme === 'dark';
    const state = await page.evaluate(() => {
      const bg = getComputedStyle(document.body).backgroundColor;
      const match = bg.match(/\d+/g)?.map(Number) ?? [255, 255, 255];
      const luminance = (0.2126 * match[0] + 0.7152 * match[1] + 0.0722 * match[2]) / 255;
      return { prefersDark: matchMedia('(prefers-color-scheme: dark)').matches, luminance };
    });
    expect(state.prefersDark).toBe(dark);
    if (dark) expect(state.luminance, 'dark page background').toBeLessThan(0.2);
    else expect(state.luminance, 'light page background').toBeGreaterThan(0.8);
    const meta = page.locator('meta[name="color-scheme"]');
    await expect(meta).toHaveAttribute('content', 'light dark');
  });
});

test.describe('posture matrices keep icon and text alongside colour', () => {
  for (const route of ['/concepts/capability-contracts/', '/reference/security/']) {
    test(route, async ({ page }) => {
      await page.goto(href(route));
      const cells = page.locator('[data-posture-matrix] tbody td');
      const count = await cells.count();
      expect(count).toBeGreaterThan(0);
      for (let i = 0; i < count; i += 1) {
        const cell = cells.nth(i);
        await expect(cell.locator('svg').first()).toBeAttached();
        const text = (await cell.innerText()).trim();
        expect(text.length, `cell ${i} has a text label`).toBeGreaterThan(0);
      }
    });
  }
});

test.describe('home capability stack', () => {
  test('exposes four posture dots per capability and a hidden sentence each', async ({ page }) => {
    await page.goto(href('/'));
    const stack = page.locator('[data-capability-stack]');
    await expect(stack).toHaveCount(1);
    await expect(stack.locator('[data-fill]')).toHaveCount(40);
    await expect(stack.locator('[data-posture-text]')).toHaveCount(10);
    const links = stack.locator('a[href*="/concepts/capability-contracts/#matrix-"]');
    await expect(links).toHaveCount(10);
  });

  test('the journey strip lists the four stages in order', async ({ page }) => {
    await page.goto(href('/'));
    const items = page.locator('[data-stage-journey]').first().locator('li');
    await expect(items).toHaveCount(4);
    expect(await items.evaluateAll((els) => els.map((el) => el.getAttribute('data-stage')))).toEqual([
      'learn',
      'build',
      'govern',
      'scale',
    ]);
  });
});

test.describe('doc pages', () => {
  test('carry an aria-hidden reading progress bar and a back-to-top link', async ({ page }) => {
    await page.goto(href('/projects/blueprint/readme/'));
    const bar = page.locator('[data-reading-progress]');
    await expect(bar).toHaveCount(1);
    await expect(bar).toHaveAttribute('aria-hidden', 'true');
    const box = await bar.boundingBox();
    // Fixed and transform-only: it never takes layout space.
    expect(box === null || box.height <= 4).toBe(true);
    await expect(page.getByRole('link', { name: /back to top/i })).toHaveAttribute('href', '#main-content');
  });
});
