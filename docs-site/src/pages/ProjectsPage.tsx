import { Link } from 'react-router-dom';
import { ArrowRight, ExternalLink } from 'lucide-react';
import { projects } from '../content/data';
import styles from './ProjectsPage.module.css';

export function ProjectsPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Projects</h1>
          <p>
            Four complementary projects for enterprise agentic AI.
            Each is self-contained with its own README and deployment instructions.
          </p>
        </div>
      </header>

      <section className={styles.projectsSection}>
        <div className={styles.sectionContent}>
          {projects.map((project) => (
            <article
              key={project.id}
              className={styles.projectCard}
              style={{ '--stage-color': project.color } as React.CSSProperties}
            >
              <div className={styles.projectHeader}>
                <div>
                  <span className={styles.stageBadge}>{project.stageLabel}</span>
                  <h2 className={styles.projectName}>{project.name}</h2>
                </div>
                <span className={styles.stageNumber}>{project.stageNumber}</span>
              </div>

              <p className={styles.projectDescription}>{project.description}</p>

              <div className={styles.projectMeta}>
                <div className={styles.metaItem}>
                  <h4>Best for</h4>
                  <p>{project.bestFor}</p>
                </div>
                <div className={styles.metaItem}>
                  <h4>Folder</h4>
                  <code>{project.folder}/</code>
                </div>
              </div>

              <div className={styles.projectFeatures}>
                <h4>Key Features</h4>
                <ul>
                  {project.features.map((feature) => (
                    <li key={feature}>{feature}</li>
                  ))}
                </ul>
              </div>

              <div className={styles.projectPrereqs}>
                <h4>Prerequisites</h4>
                <ul>
                  {project.prerequisites.map((prereq) => (
                    <li key={prereq}>{prereq}</li>
                  ))}
                </ul>
              </div>

              <div className={styles.projectActions}>
                <Link to={`/projects/${project.id}`} className={styles.detailLink}>
                  View details <ArrowRight size={16} />
                </Link>
                <a
                  href={`https://github.com/aws-samples/sample-ai-agent-factory/tree/main/${project.folder}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.githubLink}
                >
                  GitHub <ExternalLink size={14} />
                </a>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
