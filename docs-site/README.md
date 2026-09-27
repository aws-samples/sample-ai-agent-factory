# AI Agent Factory documentation site

Static, prerendered React + TypeScript + Vite site for the AI Agent Factory repository. It renders the sub-project READMEs and docs from this repository at build time (MDX), ships no backend, makes no runtime network requests and bundles every asset from this repo.

## Prerequisites

- Node.js 22.12 or newer locally (`.nvmrc` pins 24, which CI uses)
- npm 10 or newer
- Python 3 (for `npm run validate:static`)

## Setup

```bash
npm ci
```

## Commands

| Command | Description |
|---------|-------------|
| `npm run dev` | Dev server at http://127.0.0.1:5173/sample-ai-agent-factory/ (client-rendered, no prerender) |
| `npm run build` | Typecheck, client build, SSR build, prerender to `dist/` |
| `npm run preview` | Serve `dist/` locally at the base path; like GitHub Pages, unknown paths get `404.html` with status 404 and `/dir` redirects to `/dir/` |
| `npm run typecheck` | TypeScript for `src/`, the Node-side config, plugins and scripts, and `tests/browser` |
| `npm run lint` | ESLint, zero warnings allowed |
| `npm run test` | Vitest (jsdom, testing-library, axe) |
| `npm run test:browser` | Playwright gate against `dist/` (see Browser tests) |
| `npm run validate:static` | Fail-closed checks on `dist/` and the source tree |
| `npm run clean` | Remove generated files |

## How the build works

1. `tsc` checks `src/` and the Node-side files.
2. `vite build` writes the client bundle and `dist/index.html` (the template).
3. `vite build --ssr src/entry-server.tsx --outDir dist-ssr` writes a Node bundle exporting `render(url)`.
4. `node scripts/prerender.mjs` enumerates every route from `src/routes.tsx` (`staticPaths()`, which expands `:projectId` from `src/content/data.ts` and includes every entry of `src/content/docs.ts`), renders each one with `react-dom/server`, injects `<title>`, description, canonical, Open Graph, Twitter and sitemap tags into the template, links every CSS file under `dist/assets` in the head (so lazily loaded routes paint styled, with or without JavaScript) and writes `dist/<path>/index.html`. It also writes `dist/404.html` (noindex), `dist/sitemap.xml`, `dist/llms.txt`, redirect stubs for the legacy paths, copies `src/assets/social-card.png` when present and deletes `dist-ssr/`. It exits non-zero if a route renders the not-found page, throws, sets a duplicate title, logs a console warning or is written without one of the emitted stylesheets.

On the client, `src/entry-client.tsx` preloads the lazy route modules for the current URL, then hydrates the prerendered HTML with `createBrowserRouter` (basename from `import.meta.env.BASE_URL`). In the dev server there is no prerendered HTML, so it falls back to `createRoot`.

Legacy hash URLs such as `/#/security` are handled by an inline script in `index.html` that rewrites them to the new paths before the app loads.

## Routing and pages

- `src/routes.tsx` is the single route table. Hub pages are static imports; rendered Markdown pages use route `lazy` so the main bundle stays small.
- `src/paths.ts` holds the canonical route constants (always with a trailing slash) and the legacy redirect map. Use them in `<Link to>`.
- Every page renders exactly one `<PageMeta title description />` (`src/components/PageMeta.tsx`). The prerender reads it for the head tags; the client sets `document.title`.
- `src/components/RouteChange.tsx` scrolls to the top, focuses `<main id="main-content">` and announces the new page title on navigation.

## Rendering repository Markdown

Markdown from the sibling project folders is compiled with `@mdx-js/rollup` (`format: 'md'`, `remark-gfm`, `rehype-slug`, `rehype-mdx-import-media`) plus six local plugins in `src/mdx/`:

- `remarkStripLeadingH1AndBadges` drops the first h1 and badge-only paragraphs, turns any externally hosted image into a text link, and closes heading-level gaps.
- `remarkRewriteLinks` turns relative `.md` links that map to a site page into site routes and every other relative link into an absolute GitHub link.
- `remarkToc` assigns GitHub-style heading ids and exports `toc` from the module.
- `remarkDetails` turns `<details><summary>Title</summary>` blocks into a bold paragraph plus the block's Markdown body (raw HTML would otherwise be dropped).
- `remarkTableLabels` names each table's scroll region after the nearest preceding heading (unique per document) so axe landmark rules pass.
- `remarkImageSize` reads each local image's width and height (`image-size`) so the rendered `<img>` reserves its space and fragment links land on the right heading.

Raw HTML in Markdown is dropped (no `rehype-raw`). Compiled modules receive `components={{ pre: CodeBlock, a: SmartLink, img: DocImage, table: ResponsiveTable, th: TableHeaderCell }}`.

### Adding a doc

1. Add an entry to `src/content/docs.ts` (project, slug, `sourcePath`, `route`).
2. Add a matching loader to `src/docs/docModules.ts` (`'path/to/FILE.md': () => import('../../../path/to/FILE.md')`).
3. Run `npm test` (a test asserts the two lists match) and `npm run build`.

The title comes from the file's first h1 at build time via the `virtual:repo-index` module (`src/vite-plugins/repo-index.ts`), which also exposes the workshop notebooks and the Cedar policy files.

## Browser tests

`npm run test:browser` runs the Playwright gate in `tests/browser/` (behaviour, accessibility with axe, content rules) against the prerendered `dist/` on a local `vite preview` server, at 390 px and 1440 px.

```bash
npx playwright install chromium   # once per machine
npm run build                     # the gate tests dist/, so build first
npm run test:browser
```

The preview server listens on port 4173 by default (CI). Set `PREVIEW_PORT=<free port>` when another preview is already running on this machine, for example `PREVIEW_PORT=4182 npm run test:browser`.

## GitHub Pages

The site is built with base path `/sample-ai-agent-factory/`. Before the first deployment, a repository maintainer must select GitHub Actions as the Pages source under Settings, Pages. The workflow intentionally does not enable or modify repository settings.

## Project structure

```
docs-site/
  index.html              Template (default meta, favicon, legacy hash shim)
  scripts/prerender.mjs   Build-time prerender
  scripts/validate_static.py
  src/
    entry-client.tsx      Hydration entry
    entry-server.tsx      render(url) for the prerender
    routes.tsx            Route table and staticPaths()
    paths.ts, nav.ts      Route constants, top navigation
    components/           Layout, PageMeta, RouteChange, ExternalLink, HomeLink, CodeBlock, SmartLink,
                          DocImage, ResponsiveTable, TableHeaderCell, DocPage, Breadcrumbs, PageHeader,
                          SectionNav, StageBadge, FactsTable, SourceLink, Callout, Figure
    content/              Typed content model (data.ts, docs.ts, ...)
    docs/                 MDX loader map
    mdx/                  remark plugins
    vite-plugins/         virtual:repo-index
    pages/<section>/      Page components
    styles/tokens.css     Design tokens and global styles
    types/                Ambient declarations (*.md, virtual module, inert, build constants)
```

## Validation

Before committing:

```bash
npm run typecheck
npm run lint
npm run test
npm run build
npm run validate:static
npm audit --omit=dev --audit-level=high
```

## Assets

The repository atlas SVG (`assets/repository-atlas-journey.svg`) is served from Vite's `publicDir` (the repository `assets/` folder). Every other image is imported from this repository and bundled.
