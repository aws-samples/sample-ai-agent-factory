import { readdirSync, readFileSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';

/**
 * Guardrails for the site's motion: the hero canvas background, the CSS entrance
 * animation and the scroll reveal. Every effect must respect prefers-reduced-motion,
 * offer a pause control, and never move layout.
 */

const HOME = '/sample-ai-agent-factory/';
const DIST_ASSETS = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dist', 'assets');
const CANVAS_BUDGET_GZIP_BYTES = 8 * 1024;

async function installLayoutShiftObserver(page: Page) {
  await page.addInitScript(() => {
    (window as unknown as { __cls: number }).__cls = 0;
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as (PerformanceEntry & { value: number; hadRecentInput: boolean })[]) {
        if (!entry.hadRecentInput) (window as unknown as { __cls: number }).__cls += entry.value;
      }
    }).observe({ type: 'layout-shift', buffered: true });
  });
}

test.describe('hero background animation', () => {
  test('starts running, exposes a working pause control, and pauses off-screen', async ({ page }) => {
    await installLayoutShiftObserver(page);
    await page.goto(HOME, { waitUntil: 'networkidle' });

    const root = page.locator('[data-motion]').first();
    await expect(root).toHaveAttribute('data-motion', 'running', { timeout: 5_000 });

    const canvas = root.locator('canvas');
    await expect(canvas).toHaveAttribute('aria-hidden', 'true');

    const toggle = root.getByRole('button', { name: /pause background/i });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    const box = await toggle.boundingBox();
    expect(box, 'pause control has a bounding box').not.toBeNull();
    expect(Math.min(box!.width, box!.height), 'pause control meets the 24 px target size').toBeGreaterThanOrEqual(24);

    await toggle.click();
    await expect(root).toHaveAttribute('data-motion', 'paused');
    await expect(root.getByRole('button', { name: /play background/i })).toHaveAttribute('aria-pressed', 'true');

    await root.getByRole('button', { name: /play background/i }).click();
    await expect(root).toHaveAttribute('data-motion', 'running');

    await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' }));
    await expect(root).toHaveAttribute('data-motion', 'paused', { timeout: 3_000 });
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await expect(root).toHaveAttribute('data-motion', 'running', { timeout: 3_000 });

    const cls = await page.evaluate(() => (window as unknown as { __cls: number }).__cls);
    expect(cls, 'the background must not move layout').toBeLessThan(0.02);
  });

  test('renders a single static frame when the user prefers reduced motion', async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    await page.goto(HOME, { waitUntil: 'networkidle' });
    const root = page.locator('[data-motion]').first();
    await expect(root).toHaveAttribute('data-motion', 'static', { timeout: 5_000 });
    // No CSS keyframe animation runs anywhere on the page under reduced motion
    // (transitions are excluded: they only ever answer a user's own hover or click).
    const running = await page.evaluate(() =>
      document.getAnimations().filter((a): a is CSSAnimation => a instanceof CSSAnimation && a.playState === 'running').map((a) => a.animationName),
    );
    expect(running, 'no CSS keyframe animation runs under prefers-reduced-motion').toEqual([]);
    // The user may still opt in explicitly.
    await root.getByRole('button', { name: /play background/i }).click();
    await expect(root).toHaveAttribute('data-motion', 'running');
    await context.close();
  });

  test('the canvas chunk stays within its size budget and the main bundle does not carry it', () => {
    const files = readdirSync(DIST_ASSETS);
    const chunk = files.find((f) => /^Constellation-.*\.js$/.test(f));
    expect(chunk, 'the constellation is code-split into its own chunk').toBeDefined();
    const gz = gzipSync(readFileSync(join(DIST_ASSETS, chunk!))).length;
    expect(gz, `Constellation chunk gzip size ${gz} B`).toBeLessThan(CANVAS_BUDGET_GZIP_BYTES);
    const main = files.find((f) => /^index-.*\.js$/.test(f));
    expect(main).toBeDefined();
    const mainSource = readFileSync(join(DIST_ASSETS, main!), 'utf8');
    expect(mainSource.includes('requestAnimationFrame(') && mainSource.includes('createRadialGradient'), 'canvas drawing code must not live in the main bundle').toBe(false);
    expect(statSync(join(DIST_ASSETS, main!)).size).toBeGreaterThan(0);
  });
});

test.describe('entrance and scroll reveal', () => {
  test('hero entrance settles quickly and content is fully visible afterwards', async ({ page }) => {
    await page.goto(HOME, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1_600);
    const opacities = await page.evaluate(() =>
      ['h1', 'header p', 'header a'].map((sel) => {
        const el = document.querySelector(sel);
        return el ? Number(getComputedStyle(el).opacity) : 1;
      }),
    );
    for (const o of opacities) expect(o).toBe(1);
  });

  test('scroll reveal moves blocks into place without ever fading text', async ({ page, browser }) => {
    await page.goto(HOME, { waitUntil: 'networkidle' });
    const count = await page.locator('[data-reveal]').count();
    expect(count, 'Home marks at least one block for reveal').toBeGreaterThan(0);
    // Before any scrolling, every revealed block (in view or not) is fully opaque: the
    // reveal is a rise, not a fade, so contrast never depends on scroll position.
    const fadedAtLoad = await page.evaluate(() =>
      [...document.querySelectorAll('[data-reveal]')].filter((el) => Number(getComputedStyle(el).opacity) < 0.99).length,
    );
    expect(fadedAtLoad, 'no revealed block is faded at load').toBe(0);
    // After scrolling the whole page, every revealed block in view sits at rest.
    await page.evaluate(async () => {
      for (let y = 0; y <= document.documentElement.scrollHeight; y += 300) {
        window.scrollTo({ top: y, behavior: 'instant' });
        await new Promise((r) => setTimeout(r, 40));
      }
    });
    const displaced = await page.evaluate(() =>
      [...document.querySelectorAll('[data-reveal]')].filter((el) => {
        const r = el.getBoundingClientRect();
        const fullyVisible = r.top >= 0 && r.bottom <= window.innerHeight;
        const t = getComputedStyle(el).translate;
        return fullyVisible && !(t === 'none' || /^0px( 0px)?$/.test(t));
      }).length,
    );
    expect(displaced, 'every revealed block fully in view has settled').toBe(0);

    const context = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1440, height: 900 } });
    const reduced = await context.newPage();
    await reduced.goto(HOME, { waitUntil: 'networkidle' });
    const anyMoved = await reduced.evaluate(() =>
      [...document.querySelectorAll('[data-reveal]')].some((el) => {
        const t = getComputedStyle(el).translate;
        return !(t === 'none' || /^0px( 0px)?$/.test(t));
      }),
    );
    expect(anyMoved, 'reduced motion shows every block at rest without scrolling').toBe(false);
    await context.close();
  });

  test('the Atlas SVG animation is declarative and respects reduced motion', async ({ page, browser }) => {
    const svgUrl = `${HOME}repository-atlas-journey.svg`;
    const response = await page.request.get(svgUrl);
    expect(response.status()).toBe(200);
    const body = await response.text();
    expect(body).toContain('prefers-reduced-motion: no-preference');
    expect(body).not.toMatch(/<script|on[a-z]+=/i);

    const context = await browser.newContext({ reducedMotion: 'reduce' });
    const reduced = await context.newPage();
    await reduced.goto(svgUrl);
    const animating = await reduced.evaluate(() => document.getAnimations().length);
    expect(animating, 'no SVG animation runs under prefers-reduced-motion').toBe(0);
    await context.close();
  });
});
