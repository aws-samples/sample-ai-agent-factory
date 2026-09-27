# AI Agent Factory Documentation Site

Static React+TypeScript+Vite site for the AI Agent Factory repository.

## Prerequisites

- Node.js 20.19 or newer
- npm 10+

## Setup

```bash
npm ci
```

## Commands

| Command | Description |
|---------|-------------|
| `npm run dev` | Start dev server at http://127.0.0.1:5173 |
| `npm run build` | TypeScript check + production build to `dist/` |
| `npm run preview` | Serve `dist/` locally for testing |
| `npm run typecheck` | TypeScript type checking only |
| `npm run lint` | ESLint check |
| `npm run test` | Run Vitest tests |
| `npm run test:watch` | Run tests in watch mode |
| `npm run clean` | Remove generated files (dist, cache) |

## GitHub Pages

The site is configured with base path `/sample-ai-agent-factory/` for GitHub Pages deployment.

Before the first deployment, a repository maintainer must select **GitHub Actions** as the Pages source under **Settings → Pages**. The workflow intentionally does not enable or modify repository settings.

Hash routing (`/#/path`) enables deep linking without server-side routing.

## Project Structure

```
docs-site/
├── src/
│   ├── components/    # Layout and shared components
│   ├── content/       # Data and content definitions
│   ├── pages/         # Route page components
│   └── styles/        # CSS tokens and global styles
├── index.html         # Entry point
├── vite.config.ts     # Vite configuration
└── vitest.config.ts   # Test configuration
```

## Validation

Before committing:

```bash
npm run typecheck   # Type errors
npm run lint        # Lint errors
npm run test        # Test failures
npm run build       # Build errors
npm audit --omit=dev --audit-level=high  # Security
```

## Assets

The repository atlas SVG (`assets/repository-atlas-journey.svg`) is copied from the repository root during build via Vite's `publicDir` configuration.
