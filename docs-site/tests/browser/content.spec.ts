/**
 * Content gates: sourced facts on every project page and wording rules from the
 * repository owner. Rendered text does not depend on the viewport, so the config
 * runs this file in the desktop project only.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';
import { PROJECT_IDS, href, isVerbatimMarkdownRoute, sweepRoutes } from './routes';

const { routes } = sweepRoutes();

const REQUIRED_FACT_ROWS = ['Validated regions', 'First deploy', 'Cost', 'Teardown'];

const WORKSHOP_CATALOG_URL = 'catalog.us-east-1.prod.workshops.aws/workshops/3f49be39-c62b-40a2-975b-be9bf626526a';

/**
 * The workshop's own three tracks may be named only where the workshop itself is described:
 * its project page and verbatim README, its card on the Projects index, its quickstart on Start,
 * and the glossary entry that defines "Fast Path" as a workshop-only term (IMPL-PLAN glossary).
 * Every other route, in particular Home and /start/which-project/, stays strict.
 */
const WORKSHOP_TRACK_NAMES = ['Fast Path', 'Build the Platform', 'Full Journey'];
const WORKSHOP_TRACK_ALLOWED_ROUTES = new Set([
  '/projects/workshop/',
  '/projects/workshop/readme/',
  '/projects/',
  '/start/',
  '/concepts/glossary/',
]);

/** Forbidden on every page, including verbatim Markdown. */
const FORBIDDEN_EVERYWHERE: ReadonlyArray<{ label: string; pattern: RegExp }> = [
  { label: '"not yet published"', pattern: /not yet published/i },
  { label: 'other repository name', pattern: /sample-agentcore-lowcode-nocode/i },
];

/** Forbidden in site-authored copy. Verbatim README and docs pages are exempt (their sources use these words). */
const FORBIDDEN_IN_SITE_COPY: ReadonlyArray<{ label: string; pattern: RegExp }> = [
  { label: 'em-dash (U+2014)', pattern: /\u2014/ },
  { label: '"upstream"', pattern: /\bupstream\b/i },
  { label: '"mirror"', pattern: /\bmirror/i },
  { label: '"low-code" or "no-code"', pattern: /\b(?:low|no)[- ]?code\b/i },
];

/** Everything the page renders, including code samples. */
async function renderedText(page: Page): Promise<string> {
  return page.evaluate(() => (document.body.textContent ?? '').replace(/\s+/g, ' '));
}

/**
 * Site-authored prose only. Code samples (`pre`, `code`, `kbd`, `samp`) are repository source rendered
 * verbatim, so they are removed before the wording rules for site copy are applied. Repository-wide bans
 * (FORBIDDEN_EVERYWHERE) still run against the full rendered text and markup.
 */
async function proseText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll('pre, code, kbd, samp, script, style').forEach((element) => element.remove());
    return (clone.textContent ?? '').replace(/\s+/g, ' ');
  });
}

function factsRegion(page: Page): Locator {
  const labelled = page.getByRole('region', { name: /facts/i });
  const headed = page.locator('section').filter({ has: page.getByRole('heading', { name: /^facts\b/i }) });
  return labelled.or(headed).first();
}

function factRow(region: Locator, label: string): Locator {
  // A table row or a wrapped <dt>/<dd> pair whose text starts with the label.
  return region.locator('tr, dl > div').filter({ hasText: new RegExp(`^\\s*${label}`, 'i') }).first();
}

test.describe('project facts', () => {
  for (const id of PROJECT_IDS) {
    test(`/projects/${id}/ has a Facts region whose rows cite a source`, async ({ page }) => {
      await page.goto(href(`/projects/${id}/`));
      const facts = factsRegion(page);
      await expect(facts, 'a region or section named "Facts"').toHaveCount(1);

      for (const label of REQUIRED_FACT_ROWS) {
        const row = factRow(facts, label);
        await expect(row, `Facts row "${label}"`).toHaveCount(1);
        const sourceLinks = await row.locator('a[href^="https://github.com/"]').count();
        const text = ((await row.textContent()) ?? '').replace(/\s+/g, ' ').trim();
        expect(
          sourceLinks > 0 || /not documented/i.test(text),
          `Facts row "${label}" must link a github.com source or say "not documented": ${text}`,
        ).toBe(true);
      }
    });
  }
});

test.describe('workshop page', () => {
  test('links to the published workshop in the AWS workshop catalog', async ({ page }) => {
    await page.goto(href('/projects/workshop/'));
    await expect(page.locator(`a[href*="${WORKSHOP_CATALOG_URL}"]`).first()).toBeVisible();
  });

  test('names the workshop tracks', async ({ page }) => {
    await page.goto(href('/projects/workshop/'));
    const text = await renderedText(page);
    for (const name of WORKSHOP_TRACK_NAMES) {
      expect(text, `workshop page should describe its "${name}" track`).toContain(name);
    }
  });
});

test.describe('cost wording', () => {
  for (const route of ['/', '/start/costs-and-cleanup/']) {
    test(`${route} does not mention Aurora`, async ({ page }) => {
      await page.goto(href(route));
      expect(await renderedText(page)).not.toMatch(/Aurora/);
    });
  }
});

test.describe('wording rules', () => {
  for (const route of routes) {
    test(`${route} follows the wording rules`, async ({ page }) => {
      await page.goto(href(route));
      await expect(page.locator('h1')).toHaveCount(1);
      const text = await renderedText(page);
      const prose = await proseText(page);
      const html = await page.evaluate(() => document.documentElement.outerHTML);

      for (const { label, pattern } of FORBIDDEN_EVERYWHERE) {
        expect(text, `${label} must not appear on ${route}`).not.toMatch(pattern);
      }
      expect(html, `markup on ${route} must not reference the other repository`).not.toMatch(/sample-agentcore-lowcode-nocode/i);

      if (!isVerbatimMarkdownRoute(route)) {
        for (const { label, pattern } of FORBIDDEN_IN_SITE_COPY) {
          const match = prose.match(pattern);
          expect(match, `${label} found in site copy on ${route}: ...${excerpt(prose, match?.index)}...`).toBeNull();
        }
      }

      if (!WORKSHOP_TRACK_ALLOWED_ROUTES.has(route)) {
        for (const name of WORKSHOP_TRACK_NAMES) {
          expect(prose, `"${name}" is a workshop track name and may only appear on the workshop page`).not.toContain(name);
        }
      }
    });
  }
});

function excerpt(text: string, index: number | undefined): string {
  if (index === undefined) {
    return '';
  }
  return text.slice(Math.max(0, index - 60), index + 60);
}
