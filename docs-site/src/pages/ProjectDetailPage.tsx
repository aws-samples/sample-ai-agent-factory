import { useParams, Link, Navigate } from 'react-router-dom';
import { ArrowLeft, ExternalLink, Github, FolderOpen } from 'lucide-react';
import { getProjectById, projects } from '../content/data';
import styles from './ProjectDetailPage.module.css';

export function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = projectId ? getProjectById(projectId) : undefined;

  if (!project) {
    return <Navigate to="/projects" replace />;
  }

  const projectIndex = projects.findIndex((p) => p.id === project.id);
  const prevProject = projectIndex > 0 ? projects[projectIndex - 1] : null;
  const nextProject = projectIndex < projects.length - 1 ? projects[projectIndex + 1] : null;

  const githubUrl = `https://github.com/aws-samples/sample-ai-agent-factory/tree/main/${project.folder}`;
  const readmeUrl = `https://github.com/aws-samples/sample-ai-agent-factory/blob/main/${project.folder}/README.md`;

  // For the blueprint, we have architecture SVGs
  const isBlueprint = project.id === 'blueprint';

  return (
    <div className={styles.page}>
      <header
        className={styles.header}
        style={{ '--stage-color': project.color } as React.CSSProperties}
      >
        <div className={styles.headerContent}>
          <Link to="/projects" className={styles.backLink}>
            <ArrowLeft size={18} /> All Projects
          </Link>
          <span className={styles.stageBadge}>{project.stageLabel}</span>
          <h1 className={styles.title}>{project.name}</h1>
          <p className={styles.subtitle}>{project.description}</p>
          <div className={styles.headerLinks}>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className={styles.headerLink}>
              <Github size={18} /> View on GitHub <ExternalLink size={14} />
            </a>
            <a href={readmeUrl} target="_blank" rel="noopener noreferrer" className={styles.headerLink}>
              <FolderOpen size={18} /> README <ExternalLink size={14} />
            </a>
          </div>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentContent}>
          <section className={styles.section}>
            <h2>Overview</h2>
            <p className={styles.bestFor}>
              <strong>Best for:</strong> {project.bestFor}
            </p>
            <div className={styles.metaGrid}>
              <div className={styles.metaItem}>
                <h4>Stage</h4>
                <p>{project.stageNumber}. {project.stageLabel}</p>
              </div>
              <div className={styles.metaItem}>
                <h4>Folder</h4>
                <code>{project.folder}/</code>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Key Features</h2>
            <ul className={styles.featureList}>
              {project.features.map((feature) => (
                <li key={feature}>{feature}</li>
              ))}
            </ul>
          </section>

          <section className={styles.section}>
            <h2>Highlights</h2>
            <div className={styles.highlightsGrid}>
              {project.highlights.map((highlight) => (
                <div key={highlight} className={styles.highlightCard}>
                  {highlight}
                </div>
              ))}
            </div>
          </section>

          <section className={styles.section}>
            <h2>Prerequisites</h2>
            <ul className={styles.prereqList}>
              {project.prerequisites.map((prereq) => (
                <li key={prereq}>{prereq}</li>
              ))}
            </ul>
          </section>

          {isBlueprint && (
            <section className={styles.section}>
              <h2>Architecture Diagrams</h2>
              <p className={styles.archNote}>
                The blueprint includes detailed architecture diagrams showing the enterprise operating model and AWS service-level implementation.
              </p>
              <div className={styles.archLinks}>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-concept.svg"
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.archLink}
                >
                  Operating Model Diagram <ExternalLink size={14} />
                </a>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-aws-services.svg"
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.archLink}
                >
                  AWS Services Diagram <ExternalLink size={14} />
                </a>
              </div>
            </section>
          )}

          <section className={styles.section}>
            <h2>Getting Started</h2>
            <p>
              See the project's{' '}
              <a href={readmeUrl} target="_blank" rel="noopener noreferrer">
                README.md
              </a>{' '}
              for complete deployment instructions, prerequisites, and usage documentation.
            </p>
            <div className={styles.ctaButtons}>
              <a href={readmeUrl} target="_blank" rel="noopener noreferrer" className={styles.ctaPrimary}>
                Read the Documentation <ExternalLink size={16} />
              </a>
              <a href={githubUrl} target="_blank" rel="noopener noreferrer" className={styles.ctaSecondary}>
                Browse Source Code
              </a>
            </div>
          </section>

          <nav className={styles.projectNav}>
            {prevProject && (
              <Link
                to={`/projects/${prevProject.id}`}
                className={styles.navLink}
                style={{ '--stage-color': prevProject.color } as React.CSSProperties}
              >
                <span className={styles.navLabel}>Previous</span>
                <span className={styles.navProject}>
                  <ArrowLeft size={16} /> {prevProject.shortName}
                </span>
              </Link>
            )}
            {nextProject && (
              <Link
                to={`/projects/${nextProject.id}`}
                className={`${styles.navLink} ${styles.navLinkNext}`}
                style={{ '--stage-color': nextProject.color } as React.CSSProperties}
              >
                <span className={styles.navLabel}>Next</span>
                <span className={styles.navProject}>
                  {nextProject.shortName} <ExternalLink size={16} />
                </span>
              </Link>
            )}
          </nav>
        </div>
      </div>
    </div>
  );
}
