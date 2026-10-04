/**
 * Accessibility sweep: every URL in dist/sitemap.xml, at both viewports.
 *
 * Each page must render exactly one h1 and one main#main-content before axe runs,
 * so a blank page can never pass. axe runs with WCAG 2.0/2.1/2.2 A and AA plus
 * best-practice rules, colour contrast enabled (the default). Any violation fails.
 */
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { KNOWN_ROUTES, SITE_NAME, href, sweepRoutes } from './routes';

type Violations = Awaited<ReturnType<AxeBuilder['analyze']>>['violations'];

const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa', 'best-practice'];

const { routes, sitemap } = sweepRoutes();

/**
 * Let time-based entrance animations finish before auditing, so axe measures the
 * settled page rather than a frame of the hero mid-fade. Scroll-driven animations
 * (which never "finish") and the canvas background are left alone; the reveal
 * moves without fading, so it cannot affect contrast in any state.
 */
async function settleAnimations(page: Page) {
  await page.evaluate(() =>
    Promise.race([
      Promise.all(
        document
          .getAnimations()
          .filter((animation) => animation.timeline instanceof DocumentTimeline)
          .map((animation) => animation.finished.catch(() => undefined)),
      ),
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]),
  );
}

function summarise(violations: Violations) {
  return violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    helpUrl: violation.helpUrl,
    nodes: violation.nodes.map((node) => ({
      target: node.target,
      html: node.html.slice(0, 300),
      failureSummary: node.failureSummary,
      // For colour contrast this carries fgColor, bgColor, contrastRatio and expectedContrastRatio.
      data: [...node.any, ...node.all, ...node.none].map((check) => ({ id: check.id, message: check.message, data: check.data })),
    })),
  }));
}

test.describe('sitemap', () => {
  test('dist/sitemap.xml exists and lists every known route', () => {
    expect(sitemap.ok, sitemap.ok ? undefined : sitemap.error).toBe(true);
    if (!sitemap.ok) {
      return;
    }
    const missing = KNOWN_ROUTES.filter((route) => !sitemap.routes.includes(route));
    expect(missing, 'routes from the information architecture that are missing from sitemap.xml').toEqual([]);
  });
});

for (const route of routes) {
  test(`accessibility sweep ${route}`, async ({ page }, testInfo) => {
    await page.goto(href(route));

    // A prerendered page: one landmark, one heading, a real title. Guards against a blank page.
    await expect(page.locator('main#main-content')).toHaveCount(1);
    await expect(page.locator('h1')).toHaveCount(1);
    await expect(page.locator('h1')).not.toBeEmpty();
    await expect(page).toHaveTitle(new RegExp(SITE_NAME));
    await settleAnimations(page);

    const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();

    if (results.violations.length > 0) {
      await testInfo.attach(`axe-violations-${testInfo.project.name}${route.replace(/\//g, '_')}.json`, {
        body: JSON.stringify({ url: page.url(), viewport: page.viewportSize(), violations: summarise(results.violations) }, null, 2),
        contentType: 'application/json',
      });
    }

    const report = results.violations.map((violation) => {
      const targets = violation.nodes.map((node) => node.target.join(' ')).join(' | ');
      return `${violation.id} [${violation.impact}] ${violation.help}: ${targets}`;
    });
    expect(report, `axe violations on ${route} at ${testInfo.project.name} (details in the attached JSON)`).toEqual([]);
  });
}
