import { Card } from '../../components/Card';
import { ChipNav } from '../../components/ChipNav';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SmartLink } from '../../components/SmartLink';
import { Sources } from '../../components/Sources';
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
        description="Answers to common questions about the Agentic AI Factory samples: publication, regions, costs, first sign-in, production readiness and the open GitHub issues, each with its repository source."
      />
      <PageHeader
        eyebrow="Start"
        title="Frequently asked questions"
        lead="Answers about the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore, seeded from the README notices and the open GitHub issues. Each answer links to the repository text it rests on."
      />
      <div className="container">
        <StartNav />

        <ChipNav
          label="Questions"
          lead="Questions:"
          overflow="wrap"
          className={styles.faqNav}
          items={faq.map((entry) => ({ href: `#${entry.id}`, label: entry.question }))}
        />

        <div className={styles.faqList}>
          {faq.map((entry) => (
            <Card
              as="section"
              key={entry.id}
              id={entry.id}
              aria-labelledby={`${entry.id}-heading`}
              reveal
              padding="lg"
              className={styles.faqCard}
            >
              <h2 id={`${entry.id}-heading`} className={styles.faqQuestion}>
                {entry.question}
              </h2>
              {entry.projectIds.length > 0 && (
                <ul className={styles.badgeRow} aria-label="Projects concerned">
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
              <div className={styles.prose}>
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
                <Sources sources={entry.sources} label={entry.sources.length === 1 ? 'Source' : 'Sources'} />
              </div>
            </Card>
          ))}
        </div>
      </div>
    </>
  );
}
