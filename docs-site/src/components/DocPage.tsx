import { Link } from 'react-router-dom';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { getProjectById } from '../content/data';
import { docsForProject, type DocEntry } from '../content/docs';
import { docLabel, docTitle } from '../docs/docModules';
import type { DocModule } from '../docs/types';
import { blob } from '../content/links';
import { PATHS, projectPath } from '../paths';
import { Breadcrumbs, type Crumb } from './Breadcrumbs';
import { ExternalLink } from './ExternalLink';
import { mdxComponents } from './mdxComponents';
import { PageMeta } from './PageMeta';
import styles from './DocPage.module.css';

export interface DocPageProps {
  entry: DocEntry;
  mod: DocModule;
}

/**
 * Page shell for a rendered repository Markdown file: breadcrumbs, project
 * sidebar, h1 from the file's own title, source link, TOC rail, MDX body and
 * previous/next links within the project.
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

  return (
    <div className={`container ${styles.page}`}>
      <PageMeta title={pageTitle} description={description} />
      <Breadcrumbs items={crumbs} />
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
          <header className={styles.header}>
            <h1>{title}</h1>
            <p className={styles.source}>
              <ExternalLink href={blob(entry.sourcePath)}>View source on GitHub</ExternalLink>
            </p>
          </header>

          <div className={styles.body}>
            <Content components={mdxComponents} />
          </div>

          {(previous || next) && (
            <nav aria-label="Previous and next documents" className={styles.pager}>
              {previous ? (
                <Link to={previous.route} className={styles.pagerLink} rel="prev">
                  <ArrowLeft size={16} aria-hidden="true" />
                  <span>
                    <span className={styles.pagerLabel}>Previous</span>
                    {docLabel(previous)}
                  </span>
                </Link>
              ) : (
                <span />
              )}
              {next && (
                <Link to={next.route} className={`${styles.pagerLink} ${styles.pagerNext}`} rel="next">
                  <span>
                    <span className={styles.pagerLabel}>Next</span>
                    {docLabel(next)}
                  </span>
                  <ArrowRight size={16} aria-hidden="true" />
                </Link>
              )}
            </nav>
          )}
        </article>

      </div>
    </div>
  );
}
