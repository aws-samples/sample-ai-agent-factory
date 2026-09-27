import { execSync } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import mdx from '@mdx-js/rollup';
import react from '@vitejs/plugin-react';
import rehypeMdxImportMedia from 'rehype-mdx-import-media';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import { defineConfig, type Plugin } from 'vite';
import { docs, routeForSourcePath } from './src/content/docs';
import { remarkDetails } from './src/mdx/remarkDetails';
import { remarkImageSize } from './src/mdx/remarkImageSize';
import { remarkRewriteLinks } from './src/mdx/remarkRewriteLinks';
import { remarkStripLeadingH1AndBadges } from './src/mdx/remarkStripLeadingH1AndBadges';
import { remarkTableLabels } from './src/mdx/remarkTableLabels';
import { remarkToc } from './src/mdx/remarkToc';
import { repoIndexPlugin } from './src/vite-plugins/repo-index';

const siteRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(siteRoot, '..');
const REPO_URL = 'https://github.com/aws-samples/sample-ai-agent-factory';

function git(args: string): string | undefined {
  try {
    return execSync(`git ${args}`, { cwd: repoRoot, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch {
    return undefined;
  }
}

/** 7-character short SHA of the current commit; never the full SHA. */
function gitShortSha(): string {
  return git('rev-parse --short=7 HEAD')?.slice(0, 7) || 'unknown';
}

/**
 * Commit date (YYYY-MM-DD) of HEAD. Derived from git rather than the clock so the client
 * and SSR builds, which run as separate processes, stamp the same date.
 */
function gitCommitDate(): string {
  const date = git('log -1 --format=%cs');
  return date && /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : 'unknown';
}

/**
 * Make `vite preview` answer like GitHub Pages for paths that were not prerendered:
 * dist/404.html with status 404 instead of the SPA fallback (root index.html, status 200),
 * and a 301 from `/dir` to `/dir/` when `dir/index.html` exists (including the bare base path). The hook runs before
 * Vite's own preview middlewares, so it sees the original request path.
 */
function previewLikeGitHubPages(): Plugin {
  return {
    name: 'preview-like-github-pages',
    configurePreviewServer(server) {
      const outDir = path.resolve(server.config.root, server.config.build.outDir);
      const base = server.config.base.replace(/\/$/, '');
      server.middlewares.use((req, res, next) => {
        if (req.method !== 'GET' && req.method !== 'HEAD') return next();
        const url = new URL(req.url ?? '/', 'http://localhost');
        if (base && url.pathname === base) {
          // GitHub Pages answers the bare base path with a redirect to the directory.
          res.statusCode = 301;
          res.setHeader('Location', `${base}/${url.search}`);
          return res.end();
        }
        if (!url.pathname.startsWith(`${base}/`)) return next();
        let target: string;
        try {
          target = path.resolve(outDir, `.${decodeURIComponent(url.pathname.slice(base.length))}`);
        } catch {
          return next();
        }
        // Never look outside dist/, whatever the request path spells.
        if (target !== outDir && !target.startsWith(`${outDir}${path.sep}`)) return next();
        const stat = existsSync(target) ? statSync(target) : undefined;
        if (stat?.isFile()) return next();
        if (stat?.isDirectory() && existsSync(path.join(target, 'index.html'))) {
          if (url.pathname.endsWith('/')) return next();
          res.statusCode = 301;
          res.setHeader('Location', `${url.pathname}/${url.search}`);
          return res.end();
        }
        const notFound = path.join(outDir, '404.html');
        if (!existsSync(notFound)) return next();
        res.statusCode = 404;
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.end(readFileSync(notFound));
      });
    },
  };
}

export default defineConfig({
  plugins: [
    {
      enforce: 'pre',
      ...mdx({
        format: 'md',
        include: /\.md$/,
        remarkPlugins: [
          remarkGfm,
          remarkDetails,
          remarkStripLeadingH1AndBadges,
          remarkTableLabels,
          [
            remarkRewriteLinks,
            {
              repoRoot,
              routeFor: routeForSourcePath,
              blobUrl: (p: string) => `${REPO_URL}/blob/main/${p}`,
              treeUrl: (p: string) => `${REPO_URL}/tree/main/${p}`,
            },
          ],
          remarkImageSize,
          remarkToc,
        ],
        rehypePlugins: [rehypeSlug, rehypeMdxImportMedia],
      }),
    },
    react(),
    repoIndexPlugin({ repoRoot, docs }),
    previewLikeGitHubPages(),
  ],
  base: '/sample-ai-agent-factory/',
  publicDir: path.resolve(siteRoot, '../assets'),
  define: {
    __BUILD_SHA__: JSON.stringify(gitShortSha()),
    __BUILD_DATE__: JSON.stringify(gitCommitDate()),
  },
  resolve: {
    alias: {
      '@': path.resolve(siteRoot, 'src'),
    },
    // Markdown compiled from the sibling project folders imports react/jsx-runtime;
    // dedupe makes those bare imports resolve from this package's node_modules.
    dedupe: ['react', 'react-dom'],
  },
  server: {
    fs: {
      // Markdown, images and diagrams are imported from the sibling project folders.
      allow: ['..'],
    },
  },
  build: {
    sourcemap: false,
  },
});
