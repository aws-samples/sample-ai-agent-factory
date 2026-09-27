/**
 * Behavioural gates: skip link, route-change handling, mobile menu containment,
 * Projects dropdown dismissal, legacy hash redirects, prerendered deep links,
 * new-tab link names, console cleanliness, no third-party requests, no cookies or web storage,
 * and reflow without horizontal scrolling.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  BASE_PATH,
  LEGACY_HASH_REDIRECTS,
  SITE_NAME,
  SITE_ORIGIN,
  SITE_URL,
  escapeRegExp,
  href,
  sweepRoutes,
} from './routes';

const { routes } = sweepRoutes();

function isMobile(): boolean {
  return test.info().project.name === 'mobile';
}

async function activeElement(page: Page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    return {
      tag: el?.tagName.toLowerCase() ?? null,
      id: el?.id ?? '',
      text: (el?.textContent ?? '').trim().slice(0, 60),
      isBody: el === document.body,
      inMain: Boolean(el?.closest('main')),
      inFooter: Boolean(el?.closest('footer')),
      inDialog: Boolean(el?.closest('[role="dialog"]')),
      isMenuToggle: Boolean(el?.matches('header button[aria-expanded]') && !el?.closest('[role="dialog"]')),
    };
  });
}

/** At 390 px the desktop navigation is hidden, so the menu toggle is the first visible header button with aria-expanded. */
function mobileMenuToggle(page: Page): Locator {
  return page.locator('header button[aria-expanded]:visible').first();
}

test.describe('skip link', () => {
  test('Tab then Enter keeps the route and moves focus to main', async ({ page }) => {
    await page.goto(href('/'));
    const h1Before = (await page.locator('h1').textContent())?.trim();

    await page.keyboard.press('Tab');
    await expect(page.locator(':focus'), 'first Tab stop must be the skip link').toHaveAttribute('href', /#main-content$/);
    await page.keyboard.press('Enter');

    expect(await page.evaluate(() => location.pathname)).toBe(BASE_PATH);
    await expect(page.locator('h1')).toHaveText(h1Before ?? '');
    await expect(page.locator('main#main-content')).toBeFocused();
  });
});

test.describe('route change', () => {
  test('resets scroll, updates the title and moves focus to main', async ({ page }) => {
    await page.goto(href('/'));
    const homeTitle = await page.title();
    const homeH1 = (await page.locator('h1').textContent())?.trim() ?? '';

    await page.evaluate(() => window.scrollTo({ top: 1500, behavior: 'instant' }));
    expect(await page.evaluate(() => window.scrollY), 'Home must be tall enough to scroll').toBeGreaterThan(800);

    // The Blueprint tile on Home. dispatchEvent avoids Playwright scrolling the link into view first.
    const link = page.locator('main a[href$="/projects/blueprint/"]').first();
    await expect(link, 'Home must link to /projects/blueprint/').toHaveCount(1);
    await link.dispatchEvent('click');

    await expect(page).toHaveURL(new RegExp(`${escapeRegExp(href('/projects/blueprint/'))}$`));
    await expect(page.locator('h1')).not.toHaveText(homeH1);
    await expect(page).toHaveTitle(/Blueprint/);
    await expect(page).toHaveTitle(new RegExp(SITE_NAME));
    expect(await page.title()).not.toBe(homeTitle);
    await expect.poll(() => page.evaluate(() => window.scrollY), 'scroll position after a client-side navigation').toBe(0);
    await expect(page.locator('main#main-content')).toBeFocused();
  });

  test('Back navigation keeps browser scroll restoration (soft check)', async ({ page }, testInfo) => {
    await page.goto(href('/'));
    await page.evaluate(() => window.scrollTo({ top: 900, behavior: 'instant' }));
    const before = await page.evaluate(() => window.scrollY);
    const link = page.locator('main a[href$="/projects/blueprint/"]').first();
    await link.dispatchEvent('click');
    await expect(page).toHaveURL(new RegExp(`${escapeRegExp(href('/projects/blueprint/'))}$`));

    await page.goBack();
    await expect(page).toHaveURL(SITE_URL);
    await page.locator('h1').waitFor();
    const after = await page.evaluate(() => window.scrollY);

    // Scroll restoration on POP is browser behaviour and depends on layout timing.
    // The site must not defeat it, but a miss here is reported, not failed.
    if (Math.abs(after - before) > 200) {
      testInfo.annotations.push({
        type: 'warning',
        description: `scroll not restored on Back: ${before}px before, ${after}px after`,
      });
    }
  });
});

test.describe('mobile menu', () => {
  test('keeps keyboard focus out of the page behind it and returns focus on Escape', async ({ page }) => {
    test.skip(!isMobile(), 'mobile viewport only');
    await page.goto(href('/'));

    const toggle = mobileMenuToggle(page);
    await expect(toggle).toBeVisible();
    await toggle.focus();
    await page.keyboard.press('Enter');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');

    const dialog = page.locator('[role="dialog"][aria-modal="true"]');
    await expect(dialog, 'open menu must be a modal dialog').toBeVisible();
    await expect.poll(async () => (await activeElement(page)).inDialog, 'focus moves into the menu on open').toBe(true);

    const stops: string[] = [];
    let visitedDialog = false;
    for (let i = 0; i < 40; i += 1) {
      await page.keyboard.press('Tab');
      const focus = await activeElement(page);
      stops.push(`${focus.tag}${focus.id ? `#${focus.id}` : ''} "${focus.text}"`);
      expect(focus.inMain, `Tab stop ${i + 1} landed inside main while the menu is open: ${stops.join(' > ')}`).toBe(false);
      expect(focus.inFooter, `Tab stop ${i + 1} landed inside footer while the menu is open: ${stops.join(' > ')}`).toBe(false);
      visitedDialog = visitedDialog || focus.inDialog;
      if (focus.isBody || (visitedDialog && focus.isMenuToggle)) {
        break;
      }
    }
    expect(visitedDialog, `Tab never reached a menu item: ${stops.join(' > ')}`).toBe(true);

    await page.keyboard.press('Escape');
    await expect(toggle).toBeFocused();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(dialog).toBeHidden();
  });
});

test.describe('Projects dropdown', () => {
  test('closes on outside click and when focus leaves it', async ({ page }) => {
    test.skip(isMobile(), 'desktop viewport only');
    await page.goto(href('/'));

    const trigger = page.locator('header button[aria-expanded]', { hasText: /projects/i }).first();
    await expect(trigger).toBeVisible();
    const menuId = await trigger.getAttribute('aria-controls');
    expect(menuId, 'Projects trigger must reference its menu with aria-controls').toBeTruthy();
    const menu = page.locator(`[id="${menuId}"]`);

    // Outside click: a point in the left gutter of main, away from any link.
    await trigger.click();
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await expect(menu).toBeVisible();
    const main = await page.locator('main#main-content').boundingBox();
    expect(main).not.toBeNull();
    const viewport = page.viewportSize();
    await page.mouse.click(Math.max(4, (main?.x ?? 0) + 8), Math.min((main?.y ?? 0) + 200, (viewport?.height ?? 900) - 8));
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await expect(page).toHaveURL(SITE_URL);

    // Focus leaving: Tab past the last item.
    await trigger.click();
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    let left = false;
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press('Tab');
      const inside = await page.evaluate(
        (id) => Boolean(document.activeElement?.closest(`[id="${id}"]`) || document.activeElement?.matches('header button[aria-expanded]')),
        menuId,
      );
      if (!inside) {
        left = true;
        break;
      }
    }
    expect(left, 'focus never left the Projects menu after 12 Tab presses').toBe(true);
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });
});

test.describe('legacy hash URLs', () => {
  for (const { hash, route } of LEGACY_HASH_REDIRECTS) {
    test(`${hash} lands on ${route}`, async ({ page }) => {
      // The inline head shim calls location.replace before load, so wait for commit only.
      await page.goto(href('/') + hash, { waitUntil: 'commit' });
      await expect(page).toHaveURL(SITE_URL + route.replace(/^\//, ''));
      expect(await page.evaluate(() => location.pathname)).toBe(href(route));
      expect(await page.evaluate(() => location.hash)).toBe('');
      await expect(page.locator('h1')).toHaveCount(1);
      await expect(page.locator('h1')).not.toHaveText(/not found/i);
    });
  }
});

test.describe('prerendered HTML', () => {
  test('a deep link is served as real HTML with title and canonical', async ({ request }) => {
    const response = await request.get(href('/projects/blueprint/'));
    expect(response.status()).toBe(200);
    const html = await response.text();
    expect(html).toMatch(/<title>[^<]*Blueprint[^<]*<\/title>/);
    expect(html).toMatch(/<link[^>]+rel=["']canonical["']/);
    expect(html).toMatch(/<main[^>]+id=["']main-content["']/);
    expect(html).toMatch(/<h1[\s>]/);
  });

  test('an unknown path shows the not-found page', async ({ page, request }, testInfo) => {
    const response = await request.get(href('/does-not-exist/'));
    if (response.status() === 404) {
      expect(await response.text()).toMatch(/not found/i);
    } else {
      // vite preview falls back to the root index.html with status 200 (verified with Vite 7.3.6).
      // GitHub Pages serves dist/404.html with status 404; that file is checked below.
      testInfo.annotations.push({
        type: 'note',
        description: `server answered ${response.status()} for an unknown path (SPA fallback); 404 status is only observable on GitHub Pages`,
      });
    }

    const notFound = await request.get(href('/404.html'));
    expect(notFound.status(), 'dist/404.html must exist').toBe(200);
    const body = await notFound.text();
    expect(body).toMatch(/<h1[\s>]/);
    expect(body).toMatch(/not found/i);
    expect(body).toMatch(/<meta[^>]+name=["']robots["'][^>]+content=["'][^"']*noindex|<meta[^>]+content=["'][^"']*noindex[^"']*["'][^>]+name=["']robots["']/i);

    await page.goto(href('/does-not-exist/'));
    await expect(page.locator('h1')).toHaveCount(1);
    await expect(page.locator('h1')).toHaveText(/not found/i);
  });
});

test.describe('new-tab links', () => {
  test('every target="_blank" link on Home says it opens in a new tab', async ({ page }) => {
    await page.goto(href('/'));
    const offenders = await page.locator('a[target="_blank"]').evaluateAll((anchors) =>
      anchors
        .map((anchor) => {
          const labelledBy = anchor.getAttribute('aria-labelledby');
          const labelled = labelledBy
            ? labelledBy
                .split(/\s+/)
                .map((id) => document.getElementById(id)?.textContent ?? '')
                .join(' ')
            : '';
          const clone = anchor.cloneNode(true) as HTMLElement;
          clone.querySelectorAll('[aria-hidden="true"]').forEach((node) => node.remove());
          const images = [...clone.querySelectorAll('img')].map((img) => img.getAttribute('alt') ?? '').join(' ');
          const name = [anchor.getAttribute('aria-label') ?? '', labelled, clone.textContent ?? '', images, anchor.getAttribute('title') ?? '']
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();
          return { href: anchor.getAttribute('href') ?? '', name };
        })
        .filter((link) => !/new tab/i.test(link.name)),
    );
    expect(offenders, 'target="_blank" links whose accessible name does not mention a new tab').toEqual([]);
  });
});

test.describe('console and network', () => {
  test('no console errors, page errors, bad assets, third-party requests, cookies or web storage on any route', async ({
    page,
    context,
  }) => {
    test.setTimeout(180_000);
    const problems: string[] = [];
    let current = '(startup)';
    page.on('request', (request) => {
      // Every request must stay on the site's own origin. data: URLs are inline content, not requests.
      const url = request.url();
      if (!url.startsWith(SITE_ORIGIN) && !url.startsWith('data:')) {
        problems.push(`${current}: request to another origin: ${request.method()} ${url}`);
      }
    });
    page.on('console', (message) => {
      if (message.type() === 'error') {
        problems.push(`${current}: console.error: ${message.text()}`);
      }
    });
    page.on('pageerror', (error) => {
      problems.push(`${current}: pageerror: ${error.message}`);
    });
    page.on('response', (response) => {
      const type = response.request().resourceType();
      if (!['script', 'stylesheet', 'image', 'font', 'media'].includes(type)) {
        return;
      }
      const contentType = response.headers()['content-type'] ?? '';
      if (response.status() >= 400 || contentType.includes('text/html')) {
        problems.push(`${current}: ${type} ${response.request().url()} answered ${response.status()} ${contentType}`);
      }
    });

    for (const route of routes) {
      current = route;
      await page.goto(href(route));
      await expect(page.locator('h1')).toHaveCount(1);
    }

    expect(problems).toEqual([]);
    expect(await context.cookies(), 'the site must not set cookies').toEqual([]);
    const storage = await page.evaluate(() => ({
      localStorage: Object.keys(window.localStorage),
      sessionStorage: Object.keys(window.sessionStorage),
    }));
    expect(storage, 'the site must not write to web storage').toEqual({ localStorage: [], sessionStorage: [] });
  });
});

// WCAG 2.2 SC 1.4.10 (Reflow): no route may need horizontal scrolling. 320 px is the criterion's
// floor, 390 and 1440 are the project viewports, 1100 catches the desktop layout just below its breakpoint.
const REFLOW_WIDTHS = [320, 390, 1100, 1440];

test.describe('reflow', () => {
  test(`no route overflows the viewport horizontally at ${REFLOW_WIDTHS.join(', ')} px`, async ({ page }) => {
    test.setTimeout(180_000);
    const overflowing: string[] = [];
    const height = page.viewportSize()?.height ?? 900;

    for (const route of routes) {
      await page.goto(href(route));
      await expect(page.locator('h1')).toHaveCount(1);
      for (const width of REFLOW_WIDTHS) {
        await page.setViewportSize({ width, height });
        await page.waitForFunction((expected) => window.innerWidth === expected, width);
        const { scrollWidth, innerWidth } = await page.evaluate(() => ({
          scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
          innerWidth: window.innerWidth,
        }));
        if (scrollWidth > innerWidth) {
          overflowing.push(`${route} at ${width}px: content is ${scrollWidth}px wide in a ${innerWidth}px viewport`);
        }
      }
    }

    expect(overflowing).toEqual([]);
  });
});
