import { expect, test } from '@playwright/test';
import { href } from './routes';

/** The homepage's scroll story is progressive enhancement: links and headings work before motion. */
test.describe('home journey story', () => {
  test('activates each stage with native scrolling and preserves the project picker', async ({ page }) => {
    await page.goto(href('/'));
    const story = page.locator('section[data-story-stage]').first();
    for (const stage of ['learn', 'build', 'govern', 'scale']) {
      await page.locator(`#story-${stage}`).scrollIntoViewIfNeeded();
      await page.locator(`#story-${stage}`).evaluate((element) => {
        const rect = element.getBoundingClientRect();
        window.scrollTo({ top: window.scrollY + rect.top - (window.innerHeight - rect.height) / 2, behavior: 'instant' });
      });
      await expect(story).toHaveAttribute('data-story-stage', stage);
      await expect(page.locator(`[data-scene="${stage}"] > [data-active="true"]`)).toHaveCount(1);
    }
    await page.locator('#tiles-heading').scrollIntoViewIfNeeded();
    await expect(page.getByRole('heading', { name: 'Pick a project' })).toBeVisible();
  });

  test('chapter links keep the homepage route and focus their destination', async ({ page }) => {
    await page.goto(href('/'));
    await page.getByRole('link', { name: 'Begin the journey' }).click();
    await expect(page).toHaveURL(/#story-learn$/);
    await expect(page.locator('#story-learn')).toBeInViewport();
    await expect(page.locator('h1')).toHaveText('Agentic AIFactory');
  });

  test('offers a working motion toggle and respects reduced motion', async ({ page, browser }) => {
    await page.goto(href('/'));
    const story = page.locator('section[data-story-stage]').first();
    const toggle = page.getByRole('button', { name: 'Pause story animations' });
    if (await toggle.isVisible()) {
      await toggle.click();
      await expect(story).toHaveAttribute('data-story-motion', 'static');
      await page.getByRole('button', { name: 'Play story animations' }).click();
      await expect(story).toHaveAttribute('data-story-motion', 'animated');
    }

    const context = await browser.newContext({ reducedMotion: 'reduce' });
    const reduced = await context.newPage();
    await reduced.goto(href('/'));
    await expect(reduced.locator('section[data-story-stage]').first()).toHaveAttribute('data-story-motion', 'static');
    await context.close();
  });
});
