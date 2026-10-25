/**
 * Renders src/assets/social-card.png (1200 x 630) from an inline HTML card with Playwright's
 * bundled Chromium. Run after a wording or palette change: `node scripts/social-card.mjs`.
 * The right half is the repository Atlas SVG from ../assets, so the card follows the figure.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const siteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const atlas = readFileSync(path.join(siteRoot, '..', 'assets', 'repository-atlas-journey.svg'), 'utf8');
const target = path.join(siteRoot, 'src', 'assets', 'social-card.png');

const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; margin: 0; }
  html, body { width: 1200px; height: 630px; overflow: hidden; }
  body {
    font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
    color: #F5F7FB;
    background: radial-gradient(90% 80% at 85% 20%, #16213A 0%, #0B1220 55%, #070B14 100%);
    display: grid; grid-template-columns: 500px 1fr; gap: 40px; padding: 60px 48px 60px 64px; align-items: center;
  }
  .eyebrow { font-size: 22px; font-weight: 700; letter-spacing: 0.18em; color: #94A3B8; }
  h1 { margin-top: 16px; font-size: 76px; line-height: 0.98; font-weight: 800; letter-spacing: -0.03em; }
  h1 em { font-family: Georgia, 'Times New Roman', serif; font-style: italic; font-weight: 400; font-size: 1.06em; background: linear-gradient(90deg, #93C5FD, #C4B5FD); -webkit-background-clip: text; background-clip: text; color: transparent; }
  p { margin-top: 26px; font-size: 27px; line-height: 1.3; color: #CBD5E1; }
  .pills { display: flex; gap: 12px; margin-top: 36px; }
  .pill { padding: 8px 14px; border-radius: 8px; font-size: 17px; font-weight: 800; letter-spacing: 0.08em; color: #0B1220; }
  .figure { background: #0B1220; border: 1px solid #2A3650; border-radius: 16px; padding: 4px; box-shadow: 0 24px 60px rgba(0,0,0,0.45); }
  .figure svg { display: block; width: 100%; height: auto; border-radius: 12px; }
</style></head>
<body>
  <div>
    <div class="eyebrow">AWS SAMPLES</div>
    <h1>Agentic AI<br><em>Factory</em></h1>
    <p>Enterprise agentic AI samples on Amazon Bedrock AgentCore</p>
    <div class="pills">
      <span class="pill" style="background:#FDBA74">LEARN</span>
      <span class="pill" style="background:#86EFAC">BUILD</span>
      <span class="pill" style="background:#C4B5FD">GOVERN</span>
      <span class="pill" style="background:#93C5FD">SCALE</span>
    </div>
  </div>
  <div class="figure">${atlas.replace(/^<\?xml[^>]*>\s*/, '')}</div>
</body></html>`;

const browser = await chromium.launch();
// Reduced motion makes the Atlas SVG render its settled state instead of the first frame of its entrance.
const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
await page.setContent(html, { waitUntil: 'load' });
await page.waitForTimeout(600);
const png = await page.screenshot({ type: 'png', clip: { x: 0, y: 0, width: 1200, height: 630 } });
await browser.close();
writeFileSync(target, png);
console.log(`social card: wrote ${target} (${png.length} bytes)`);
