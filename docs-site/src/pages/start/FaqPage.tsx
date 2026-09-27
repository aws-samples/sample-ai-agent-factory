import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SmartLink } from '../../components/SmartLink';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { getProjectById } from '../../content/data';
import { faq } from '../../content/faq';
import { StartNav } from './shared';
import styles from './start.module.css';

export function FaqPage() {
  return (
    <>
      <PageMeta
        title="Frequently asked questions"
        description="Answers to common questions about the AI Agent Factory samples: publication, regions, costs, first sign-in, production readiness and the open GitHub issues, each with its repository source."
      />
      <PageHeader
        eyebrow="Start"
        title="Frequently asked questions"
        lead="Answers about the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore, seeded from the README notices and the open GitHub issues. Each answer links to the repository text it rests on."
      />
      <div className="container">
        <StartNav />

        <nav aria-label="Questions" className={styles.projectSection}>
          <ol className={styles.bulletList}>
            {faq.map((entry) => (
              <li key={entry.id}>
                <a href={`#${entry.id}`}>{entry.question}</a>
              </li>
            ))}
          </ol>
        </nav>

        {faq.map((entry) => (
          <section
            key={entry.id}
            id={entry.id}
            className={styles.projectSection}
            aria-labelledby={`${entry.id}-heading`}
          >
            <h2 id={`${entry.id}-heading`}>{entry.question}</h2>
            {entry.projectIds.length > 0 && (
              <ul className={styles.chips} aria-label="Projects concerned">
                {entry.projectIds.map((projectId) => {
                  const project = getProjectById(projectId);
                  return project ? (
                    <li key={projectId}>
                      <StageBadge stage={project.stage} label={project.shortName} variant="outline" />
                    </li>
                  ) : null;
                })}
              </ul>
            )}
            <div className={`${styles.prose} ${styles.subSection}`}>
              <p>{entry.answer}</p>
              {entry.links && entry.links.length > 0 && (
                <ul className={styles.bulletList}>
                  {entry.links.map((link) => (
                    <li key={link.href}>
                      <SmartLink href={link.href}>{link.label}</SmartLink>
                    </li>
                  ))}
                </ul>
              )}
              <p className={styles.meta}>
                {entry.sources.length === 1 ? 'Source: ' : 'Sources: '}
                {entry.sources.map((source, index) => (
                  <span key={`${source.file}-${source.heading ?? ''}-${source.quote ?? index}`}>
                    {index > 0 && '; '}
                    <SourceLink source={source} />
                  </span>
                ))}
              </p>
            </div>
          </section>
        ))}
      </div>
    </>
  );
}
