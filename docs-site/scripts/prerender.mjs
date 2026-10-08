#!/usr/bin/env node
/**
 * Prerender every site route to static HTML.
 *
 * Runs after `vite build` (client, in dist/) and `vite build --ssr` (server
 * bundle, in dist-ssr/). For each route it renders the React tree with the
 * server entry, injects per-page head tags into the client index.html
 * template and writes dist/<path>/index.html. It also writes 404.html,
 * sitemap.xml, llms.txt and redirect stubs for legacy paths, copies the
 * social card when one exists, and finally deletes dist-ssr/.
 *
 * The template links only the entry stylesheet; the CSS of lazily loaded route
 * chunks is normally injected at runtime, which would leave a prerendered lazy
 * route unstyled until its chunk arrives (and unstyled without JavaScript). Every
 * CSS file emitted under dist/assets is therefore linked in the head of every page.
 *
 * Fails with a non-zero exit code if any route renders the not-found page,
 * throws, produces a duplicate title, logs a console error or warning, or is
 * written without one of the emitted stylesheets.
 */
process.env.NODE_ENV = 'production';

import { access, copyFile, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const siteRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const distDir = path.join(siteRoot, 'dist');
const ssrDir = path.join(siteRoot, 'dist-ssr');

const SITE_ORIGIN = 'https://aws-samples.github.io';
const BASE_PATH = '/sample-ai-agent-factory/';
const SITE_URL = `${SITE_ORIGIN}${BASE_PATH}`;
const SITE_NAME = 'Agentic AI Factory';
const SOCIAL_CARD_SOURCE = path.join(siteRoot, 'src', 'assets', 'social-card.png');
const ROOT_MARKER = '<div id="root"></div>';
const SAVED_TEMPLATE = path.join(ssrDir, 'template.html');

// Kept so that fail() still reaches the terminal while console.error is captured during rendering.
const reportError = console.error.bind(console);

/**
 * @param {string} message
 * @returns {never}
 */
function fail(message) {
  reportError(`prerender: ${message}`);
  return process.exit(1);
}

/** @param {string} value */
function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/** @param {string} value */
function escapeXml(value) {
  return escapeHtml(value);
}

/** @param {string} file */
async function exists(file) {
  try {
    await access(file);
    return true;
  } catch {
    return false;
  }
}

/** @param {string} routePath  Site path with trailing slash, e.g. "/start/". */
function absoluteUrl(routePath) {
  return `${SITE_ORIGIN}${BASE_PATH.replace(/\/$/, '')}${routePath}`;
}

/** @param {string} routePath */
function outputFile(routePath) {
  const relative = routePath.replace(/^\/+/, '');
  return path.join(distDir, relative, 'index.html');
}

/**
 * @param {string} template
 * @param {{ title: string, description: string }} meta
 * @param {string[]} headTags
 * @param {string} appHtml
 */
function composePage(template, meta, headTags, appHtml) {
  let html = template;

  const titleTag = /<title>[^<]*<\/title>/;
  if (!titleTag.test(html)) fail('index.html template has no <title> tag');
  html = html.replace(titleTag, `<title>${escapeHtml(meta.title)}</title>`);

  const descriptionTag = /<meta name="description" content="[^"]*"\s*\/?>/;
  if (!descriptionTag.test(html)) fail('index.html template has no description meta tag');
  html = html.replace(descriptionTag, `<meta name="description" content="${escapeHtml(meta.description)}" />`);

  if (!html.includes('</head>')) fail('index.html template has no </head>');
  html = html.replace('</head>', `    ${headTags.join('\n    ')}\n  </head>`);

  if (!html.includes(ROOT_MARKER)) fail(`index.html template does not contain ${ROOT_MARKER}`);
  html = html.replace(ROOT_MARKER, `<div id="root">${appHtml}</div>`);

  return html;
}

const STYLESHEET_LINK = /<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g;

/** @param {string} html */
function linkedStylesheets(html) {
  return new Set(Array.from(html.matchAll(STYLESHEET_LINK), (match) => match[1]));
}

/**
 * Link every CSS file emitted under dist/assets from the template, right after the entry
 * stylesheet Vite already linked. Vite's runtime preload helper skips stylesheets whose
 * href is already present, so nothing loads twice; the CSS is small enough to ship whole.
 *
 * @param {string} template
 * @returns {Promise<{ template: string, stylesheets: string[] }>}
 */
async function linkAllStylesheets(template) {
  const cssFiles = (await readdir(path.join(distDir, 'assets'))).filter((file) => file.endsWith('.css')).sort();
  if (cssFiles.length === 0) fail('dist/assets contains no CSS file; run the client build first');
  const stylesheets = cssFiles.map((file) => `${BASE_PATH}assets/${file}`);

  const linked = linkedStylesheets(template);
  const missing = stylesheets.filter((href) => !linked.has(href));
  if (missing.length === 0) return { template, stylesheets };

  const anchor = template.match(/<link rel="stylesheet"[^>]*>/);
  if (!anchor) fail('index.html template links no stylesheet; run the client build first');
  const extra = missing.map((href) => `<link rel="stylesheet" crossorigin href="${href}">`).join('\n    ');
  return { template: template.replace(anchor[0], `${anchor[0]}\n    ${extra}`), stylesheets };
}

/**
 * @param {string} html
 * @param {string[]} stylesheets
 * @param {string} label
 */
function assertStylesheetsLinked(html, stylesheets, label) {
  const linked = linkedStylesheets(html);
  const missing = stylesheets.filter((href) => !linked.has(href));
  if (missing.length > 0) fail(`${label} does not link ${missing.join(', ')}`);
}

/**
 * @param {{ title: string, description: string }} meta
 * @param {string} routePath
 * @param {boolean} socialCard
 */
function pageHeadTags(meta, routePath, socialCard) {
  const url = absoluteUrl(routePath);
  const tags = [
    `<link rel="canonical" href="${url}" />`,
    `<link rel="sitemap" type="application/xml" href="${SITE_URL}sitemap.xml" />`,
    `<meta property="og:type" content="website" />`,
    `<meta property="og:site_name" content="${SITE_NAME}" />`,
    `<meta property="og:title" content="${escapeHtml(meta.title)}" />`,
    `<meta property="og:description" content="${escapeHtml(meta.description)}" />`,
    `<meta property="og:url" content="${url}" />`,
    `<meta name="twitter:card" content="${socialCard ? 'summary_large_image' : 'summary'}" />`,
    `<meta name="twitter:title" content="${escapeHtml(meta.title)}" />`,
    `<meta name="twitter:description" content="${escapeHtml(meta.description)}" />`,
  ];
  if (socialCard) {
    tags.push(
      `<meta property="og:image" content="${SITE_URL}social-card.png" />`,
      `<meta property="og:image:width" content="1200" />`,
      `<meta property="og:image:height" content="630" />`,
      `<meta property="og:image:alt" content="${SITE_NAME} repository atlas" />`,
      `<meta name="twitter:image" content="${SITE_URL}social-card.png" />`,
    );
  }
  return tags;
}

const TITLE_SUFFIX = ` | ${SITE_NAME}`;

/** @param {string} toTitle  Destination page title without the site suffix, e.g. "Security". */
function redirectStubTitle(toTitle) {
  return `Redirecting to ${toTitle}${TITLE_SUFFIX}`;
}

/**
 * @param {string} from     Legacy path without trailing slash, e.g. "/security".
 * @param {string} to       Canonical path with trailing slash.
 * @param {string} toTitle  Destination page title without the site suffix, e.g. "Security".
 */
function redirectStub(from, to, toTitle) {
  const target = absoluteUrl(to);
  const relativeTarget = `${BASE_PATH.replace(/\/$/, '')}${to}`;
  return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>${escapeHtml(redirectStubTitle(toTitle))}</title>
    <meta name="robots" content="noindex" />
    <link rel="canonical" href="${target}" />
    <meta http-equiv="refresh" content="0; url=${relativeTarget}" />
    <!-- hash-shim -->
    <script>window.location.replace('${relativeTarget}' + window.location.search + window.location.hash);</script>
  </head>
  <body>
    <p>${escapeHtml(from)} has moved to <a href="${relativeTarget}">${escapeHtml(toTitle)}</a>.</p>
  </body>
</html>
`;
}

async function main() {
  if (!(await exists(distDir))) fail('dist/ does not exist; run `vite build` first');
  const serverEntry = path.join(ssrDir, 'entry-server.js');
  if (!(await exists(serverEntry))) fail('dist-ssr/entry-server.js does not exist; run the SSR build first');

  // dist/index.html is the template only until the home page overwrites it, so keep a
  // pristine copy in dist-ssr/ (deleted on success) to make reruns after a failure possible.
  let template = await readFile(path.join(distDir, 'index.html'), 'utf8');
  if (template.includes(ROOT_MARKER)) {
    await writeFile(SAVED_TEMPLATE, template, 'utf8');
  } else if (await exists(SAVED_TEMPLATE)) {
    template = await readFile(SAVED_TEMPLATE, 'utf8');
  } else {
    fail(`dist/index.html does not contain ${ROOT_MARKER}; run the client build first`);
  }
  const { template: pageTemplate, stylesheets } = await linkAllStylesheets(template);

  /** @type {{ render: (url: string) => Promise<{ html: string, status: number, meta: { title: string, description: string } }>, staticPaths: () => string[], REDIRECTS: Record<string, string> }} */
  const server = await import(pathToFileURL(serverEntry).href);
  const { render, staticPaths, REDIRECTS } = server;

  // Any console noise during rendering is treated as a failure.
  /** @type {string[]} */
  const consoleProblems = [];
  const originalError = console.error;
  const originalWarn = console.warn;
  console.error = (...args) => consoleProblems.push(`error: ${args.map(String).join(' ')}`);
  console.warn = (...args) => consoleProblems.push(`warn: ${args.map(String).join(' ')}`);

  const socialCard = await exists(SOCIAL_CARD_SOURCE);
  const paths = staticPaths();
  if (paths.length === 0) fail('staticPaths() returned no routes');

  /** @type {{ path: string, title: string, description: string }[]} */
  const pages = [];
  /** @type {Map<string, string>} */
  const titles = new Map();

  try {
    for (const routePath of paths) {
      const url = `${BASE_PATH.replace(/\/$/, '')}${routePath}`;
      let result;
      try {
        result = await render(url);
      } catch (error) {
        reportError(error);
        fail(`route ${routePath} threw while rendering`);
      }
      if (result.status !== 200) fail(`route ${routePath} rendered status ${result.status} (not-found page?)`);
      if (!result.meta.title || result.meta.title === SITE_NAME) {
        fail(`route ${routePath} did not set a page title via <PageMeta>`);
      }
      if (!result.meta.description) fail(`route ${routePath} did not set a description via <PageMeta>`);
      if (!result.html.includes('<main')) fail(`route ${routePath} rendered no <main> landmark`);
      const previous = titles.get(result.meta.title);
      if (previous) fail(`duplicate title "${result.meta.title}" on ${previous} and ${routePath}`);
      titles.set(result.meta.title, routePath);

      const html = composePage(pageTemplate, result.meta, pageHeadTags(result.meta, routePath, socialCard), result.html);
      assertStylesheetsLinked(html, stylesheets, `route ${routePath}`);
      const file = outputFile(routePath);
      await mkdir(path.dirname(file), { recursive: true });
      await writeFile(file, html, 'utf8');
      pages.push({ path: routePath, title: result.meta.title, description: result.meta.description });
    }

    // Real not-found page (served by GitHub Pages for unknown URLs).
    const notFound = await render(`${BASE_PATH}this-page-does-not-exist/`);
    if (notFound.status !== 404) fail(`not-found render returned status ${notFound.status}`);
    const notFoundHtml = composePage(
      pageTemplate,
      notFound.meta,
      [`<meta name="robots" content="noindex" />`],
      notFound.html,
    );
    assertStylesheetsLinked(notFoundHtml, stylesheets, '404.html');
    await writeFile(path.join(distDir, '404.html'), notFoundHtml, 'utf8');

    // Redirect stubs for legacy paths, titled after the destination page so titles stay unique.
    for (const [from, to] of Object.entries(REDIRECTS)) {
      const destination = pages.find((page) => page.path === to);
      if (!destination) fail(`redirect target ${to} for ${from} is not a prerendered route`);
      const toTitle = destination.title.endsWith(TITLE_SUFFIX)
        ? destination.title.slice(0, -TITLE_SUFFIX.length)
        : destination.title;
      const stubTitle = redirectStubTitle(toTitle);
      const previous = titles.get(stubTitle);
      if (previous) fail(`duplicate title "${stubTitle}" on ${previous} and ${from}/`);
      titles.set(stubTitle, `${from}/`);
      const file = outputFile(`${from}/`);
      await mkdir(path.dirname(file), { recursive: true });
      await writeFile(file, redirectStub(from, to, toTitle), 'utf8');
    }

    // sitemap.xml
    const sitemap = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
      ...pages.map((page) => `  <url><loc>${escapeXml(absoluteUrl(page.path))}</loc></url>`),
      '</urlset>',
      '',
    ].join('\n');
    await writeFile(path.join(distDir, 'sitemap.xml'), sitemap, 'utf8');

    // llms.txt: one line per page.
    const home = pages.find((page) => page.path === '/');
    const llms = [
      `# ${SITE_NAME}`,
      '',
      `> ${home ? home.description : 'Enterprise agentic AI samples on AWS.'}`,
      '',
      '## Pages',
      '',
      ...pages.map((page) => `- [${page.title}](${absoluteUrl(page.path)}): ${page.description}`),
      '',
    ].join('\n');
    await writeFile(path.join(distDir, 'llms.txt'), llms, 'utf8');

    if (socialCard) {
      await copyFile(SOCIAL_CARD_SOURCE, path.join(distDir, 'social-card.png'));
    }

  } finally {
    console.error = originalError;
    console.warn = originalWarn;
  }
  if (consoleProblems.length > 0) {
    for (const problem of consoleProblems) console.error(problem);
    fail(`${consoleProblems.length} console message(s) were logged during rendering`);
  }

  await rm(ssrDir, { recursive: true, force: true });

  console.log(
    `prerender: wrote ${pages.length} pages, 404.html, ${Object.keys(REDIRECTS).length} redirect stubs, sitemap.xml and llms.txt${
      socialCard ? ', social-card.png' : ' (no social card found)'
    }; ${stylesheets.length} stylesheet(s) linked on every page`,
  );
}

main().catch((error) => {
  // console.error may still be the capturing stub if rendering threw; use the preserved one.
  reportError(error);
  process.exit(1);
});
