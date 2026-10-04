/**
 * External link constants and URL helpers.
 *
 * Every outbound link on the site should be built from these so the
 * repository URL, the workshop URL and the vulnerability-reporting URL are
 * defined exactly once.
 */
import type { Source } from './facts';

/** Public GitHub repository. */
export const REPO_URL = 'https://github.com/aws-samples/sample-ai-agent-factory';

/** GitHub issue tracker for the repository. */
export const ISSUES_URL = `${REPO_URL}/issues`;

/** AWS vulnerability reporting page named in CONTRIBUTING.md and the project READMEs. */
export const VULN_REPORT_URL = 'https://aws.amazon.com/security/vulnerability-reporting/';

/** The published workshop on AWS Builder Center (Workshop Studio catalog entry). */
export const WORKSHOP_URL =
  'https://catalog.us-east-1.prod.workshops.aws/workshops/3f49be39-c62b-40a2-975b-be9bf626526a';

/** The AWS Builder Center workshop discovery listing. */
export const WORKSHOPS_DISCOVER_URL = 'https://builder.aws.com/build/workshops?tab=discover';

/** Label for the link to the workshop catalog entry. */
export const WORKSHOP_LINK_LABEL = 'Open the workshop';

/** Label for the link to the AWS Builder Center discovery listing. */
export const WORKSHOPS_DISCOVER_LABEL = 'Listed on AWS Builder Center';

/** Branch that blob and tree links point at. */
export const DEFAULT_BRANCH = 'main';

/**
 * Build a GitHub blob URL for a repository-relative file path.
 * @param path repository-relative path, for example `Agentic-ai-self-service/README.md`
 * @param anchor optional fragment without the leading `#`
 */
export function blob(path: string, anchor?: string): string {
  const clean = path.replace(/^\/+/, '');
  const base = `${REPO_URL}/blob/${DEFAULT_BRANCH}/${clean}`;
  return anchor ? `${base}#${anchor}` : base;
}

/**
 * Build a GitHub tree URL for a repository-relative folder path.
 * @param path repository-relative folder, for example `enterprise-mcp-governance-gateway/policies`
 */
export function tree(path: string): string {
  const clean = path.replace(/^\/+/, '').replace(/\/+$/, '');
  return clean ? `${REPO_URL}/tree/${DEFAULT_BRANCH}/${clean}` : `${REPO_URL}/tree/${DEFAULT_BRANCH}`;
}
/**
 * Convert a Markdown heading to the fragment GitHub generates for it.
 * Rules: trim, lowercase, drop every character that is not a letter, number,
 * space, hyphen or underscore, then turn spaces into hyphens. Markdown
 * emphasis markers and inline code backticks are removed before slugging.
 * @param heading heading text without the leading `#` characters
 */
export function githubHeadingSlug(heading: string): string {
  return heading
    .trim()
    .replace(/[`*]/g, '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N} _-]/gu, '')
    .replace(/ /g, '-');
}

/**
 * Build the URL that a `Source` points at. A `url` source returns its URL
 * unchanged; a `file` source returns the file on the default branch, with a
 * heading fragment when the source names a heading. A source with neither
 * falls back to the repository root.
 * @param source a source pointer from any content module
 */
export function githubSourceUrl(source: Source): string {
  if (source.url) return source.url;
  if (!source.file) return REPO_URL;
  return blob(source.file, source.heading ? githubHeadingSlug(source.heading) : undefined);
}
