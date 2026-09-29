import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { getProjectById, type JourneyStage, type Project } from '../content/data';
import { docsForProject, type DocEntry } from '../content/docs';
import { docLabel, docTitle } from '../docs/docModules';
import type { DocModule } from '../docs/types';
import { blob, ISSUES_URL } from '../content/links';
import { PATHS, projectPath } from '../paths';
import { BackToTop } from './BackToTop';
import { Breadcrumbs, type Crumb } from './Breadcrumbs';
import { Button } from './Button';
import { Card } from './Card';
import type { HeaderHue } from './HeaderGlow';
import { mdxComponents } from './mdxComponents';
import { PageHeader } from './PageHeader';
import { PageMeta } from './PageMeta';
import { ReadingProgress } from './ReadingProgress';
import styles from './DocPage.module.css';

export interface DocPageProps {
  entry: DocEntry;
  mod: DocModule;
}

/** What the compact header shows for one document kind. */
interface HeaderPlan {
  eyebrow: string;
  stage?: JourneyStage;
  hue?: HeaderHue;
  /** Secondary actions after "View source on GitHub". */
  extraActions: ReactNode;
}

/**
 * Header copy per DocEntry kind. Every kind keeps the "View source on GitHub" action;
 * README and doc pages add a ghost link back to the project page, and the repository-level
 * CONTRIBUTING page adds "Report an issue" instead of a project link.
 */
function headerPlan(entry: DocEntry, project: Project | undefined): HeaderPlan {
  const projectLink = project ? (
    <Button variant="ghost" size="sm" to={projectPath(project.id)}>
      Project page
    </Button>
  ) : null;
  switch (entry.kind) {
    case 'readme':
      return { eyebrow: 'README', stage: project?.stage, extraActions: projectLink };
    case 'doc':
      return { eyebrow: `${project?.shortName ?? 'Project'} docs`, stage: project?.stage, extraActions: projectLink };
    case 'changelog':
      return { eyebrow: 'Changelog', stage: project?.stage, extraActions: null };
    case 'connector':
      return { eyebrow: 'Connector', stage: project?.stage ?? 'govern', extraActions: null };
    case 'contributing':
      return {
        eyebrow: 'Repository',
        hue: 'slate',
        extraActions: (
          <Button variant="ghost" size="sm" href={ISSUES_URL} external>
            Report an issue
          </Button>
        ),
      };
  }
}

/**
 * Page shell for a rendered repository Markdown file: a compact dark header band
 * (breadcrumbs, kind eyebrow, stage badge, the file's own h1 title, source path and
 * actions), a reading progress bar, the project sidebar, TOC rail, MDX body,
 * previous/next cards within the project and a back-to-top link.
 */
export function DocPage({ entry, mod }: DocPageProps) {
  const title = docTitle(entry);
  const project = entry.project ? getProjectById(entry.project) : undefined;
  const siblings = entry.project ? docsForProject(entry.project) : [];
  const index = siblings.findIndex((d) => d.id === entry.id);
  const previous = index > 0 ? siblings[index - 1] : undefined;
  const next = index >= 0 && index < siblings.length - 1 ? siblings[index + 1] : undefined;
  const Content = mod.default;
  const toc = mod.toc ?? [];
  const plan = headerPlan(entry, project);

  const crumbs: Crumb[] = [{ label: 'Home', to: PATHS.home }];
  if (project) {
    crumbs.push({ label: 'Projects', to: PATHS.projects }, { label: project.shortName, to: projectPath(project.id) });
  }
  crumbs.push({ label: title });

  // Project landing pages already use the project name, which is usually the README h1.
  const pageTitle = entry.kind === 'readme' ? `${title} (README)` : title;

  const description = project
    ? `${title}. Rendered from ${entry.sourcePath} in the ${project.shortName} project of the AI Agent Factory repository.`
    : `${title}. Rendered from ${entry.sourcePath} in the AI Agent Factory repository.`;

  const actions = (
    <>
      <Button variant="secondary" size="sm" href={blob(entry.sourcePath)} external>
        View source on GitHub
      </Button>
      {plan.extraActions}
    </>
  );

  return (
    <>
      <PageMeta title={pageTitle} description={description} />
      <PageHeader
        variant="compact"
        breadcrumbs={<Breadcrumbs items={crumbs} />}
        eyebrow={plan.eyebrow}
        stage={plan.stage}
        hue={plan.hue}
        title={title}
        lead={
          <>
            Rendered from <span className={styles.sourcePath}>{entry.sourcePath}</span>
          </>
        }
        actions={actions}
      />
      <ReadingProgress />

      <div className={`container ${styles.page}`}>
        <div className={siblings.length > 1 ? styles.layout : styles.layoutNoSidebar}>
          {siblings.length > 1 && project && (
            <nav aria-label={`${project.shortName} documentation`} className={styles.sidebar}>
              <ul className={styles.sidebarList}>
                {siblings.map((doc) => (
                  <li key={doc.id}>
                    <Link
                      to={doc.route}
                      className={styles.sidebarLink}
                      aria-current={doc.id === entry.id ? 'page' : undefined}
                    >
                      {docLabel(doc)}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          )}

          {toc.length > 0 && (
            <nav aria-labelledby="doc-toc-heading" className={styles.toc}>
              <h2 id="doc-toc-heading" className={styles.tocHeading}>
                On this page
              </h2>
              <ul className={styles.tocList}>
                {toc.map((item) => (
                  <li key={item.id} className={item.depth === 3 ? styles.tocNested : undefined}>
                    <a href={`#${item.id}`} className={styles.tocLink}>
                      {item.text}
                    </a>
                  </li>
                ))}
              </ul>
            </nav>
          )}

          <article className={styles.article}>
            <div className={styles.body}>
              <Content components={mdxComponents} />
            </div>

            {(previous || next) && (
              <nav aria-label="Previous and next documents" className={styles.pager}>
                {previous ? (
                  <Card interactive padding="sm" className={styles.pagerCard}>
                    <Link to={previous.route} className={styles.pagerLink} rel="prev" data-stretch>
                      <ArrowLeft size={16} aria-hidden="true" />
                      <span>
                        <span className={styles.pagerLabel}>Previous</span>
                        {docLabel(previous)}
                      </span>
                    </Link>
                  </Card>
                ) : (
                  <span />
                )}
                {next && (
                  <Card interactive padding="sm" className={styles.pagerCard}>
                    <Link to={next.route} className={`${styles.pagerLink} ${styles.pagerNext}`} rel="next" data-stretch>
                      <span>
                        <span className={styles.pagerLabel}>Next</span>
                        {docLabel(next)}
                      </span>
                      <ArrowRight size={16} aria-hidden="true" />
                    </Link>
                  </Card>
                )}
              </nav>
            )}
          </article>
        </div>
      </div>

      <BackToTop />
    </>
  );
}
