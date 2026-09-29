import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { FactsTable } from '../../components/FactsTable';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ReferenceNav } from './ReferenceNav';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts, type ProjectFacts, type Source } from '../../content/facts';
import { getLimitations, validated } from '../../content/limitations';
import { PATHS, projectPath } from '../../paths';
import styles from './SupportEnvelopePage.module.css';

const ENVELOPE_SOURCE: Source = { file: 'README.md', heading: 'Support Envelope' };

/** Fact rows shown per project, in order. */
const ENVELOPE_FACT_KEYS: ReadonlyArray<keyof ProjectFacts> = ['regions', 'defaultRegion', 'status', 'version'];

/**
 * Render verbatim README text with its Markdown inline code and emphasis
 * markers turned into elements. No raw HTML is involved.
 */
function renderInline(text: string): ReactNode[] {
  const parts = text.split(/(`[^`]+`|\*[^*\s][^*]*\*)/g);
  return parts.map((part, index) => {
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

export function SupportEnvelopePage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Support envelope"
        description="Validated regions, status and the known limitations each of the four AI Agent Factory projects documents, quoted from the project READMEs with source links."
      />
      <PageHeader
        eyebrow="Reference"
        title="Support envelope"
        lead="Validated regions, status and the known limitations each project documents, collected in one place for four samples centered on Amazon Bedrock and Amazon Bedrock AgentCore."
      />

      <div className="container">
        <ReferenceNav />

        <p className={styles.intro}>
          The support envelope applies to the exact tested implementation: the reference implementations, regions and
          configurations each project names. Replacements and other regions require independent validation (
          <SourceLink source={ENVELOPE_SOURCE}>root README</SourceLink>). The limitation bullets below are quoted from
          the project files without paraphrase.
        </p>

        <nav aria-label="Projects on this page" className={styles.jumpNav}>
          <ul className={styles.jumpList}>
            {projects.map((project) => (
              <li key={project.id}>
                <a href={`#envelope-${project.id}`} className={styles.jumpLink}>
                  <StageBadge stage={project.stage} label={project.stageLabel} variant="outline" />
                  {project.shortName}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        {projects.map((project) => {
          const facts = getFacts(project.id);
          const limitations = getLimitations(project.id);
          const validatedItems = validated[project.id];
          const validatedHeading = validatedItems[0]?.source.heading ?? 'Validated reference envelope';
          return (
            <section key={project.id} className={styles.projectSection} aria-labelledby={`envelope-${project.id}`}>
              <div className={styles.projectHeading}>
                <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                <h2 id={`envelope-${project.id}`}>{project.name}</h2>
              </div>
              <p className={styles.projectLinks}>
                <Link to={projectPath(project.id)}>Project page</Link>
              </p>

              <FactsTable
                caption={`${project.shortName}: validated regions and status`}
                rows={ENVELOPE_FACT_KEYS.map((key) => ({ label: FACT_LABELS[key], fact: facts[key] }))}
              />

              {validatedItems.length > 0 && (
                <>
                  <h3 className={styles.limitationsHeading}>{validatedHeading}</h3>
                  <p className={styles.validatedIntro}>
                    What {project.shortName} states it has validated, quoted from its README (
                    <SourceLink source={validatedItems[0].source}>source</SourceLink>). Anything outside this list is
                    covered by the limitations below.
                  </p>
                  <ul className={styles.validatedList}>
                    {validatedItems.map((item) => (
                      <li key={item.id} id={item.id} className={styles.validatedItem}>
                        <blockquote className={styles.limitationText}>{renderInline(item.text)}</blockquote>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <h3 className={styles.limitationsHeading}>Known limitations, as documented</h3>
              <ul className={styles.limitationList}>
                {limitations.map((limitation) => (
                  <li key={limitation.id} id={limitation.id} className={styles.limitation} data-reveal>
                    {limitation.title && <strong className={styles.limitationTitle}>{limitation.title}</strong>}
                    <blockquote className={styles.limitationText}>{renderInline(limitation.text)}</blockquote>
                    <p className={styles.limitationSource}>
                      Source: <SourceLink source={limitation.source} />
                    </p>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}

        <p className={styles.footerNote}>
          Security controls for each project are compared on the <Link to={PATHS.referenceSecurity}>Security</Link>{' '}
          page. Costs and teardown procedures are on <Link to={PATHS.costsAndCleanup}>Costs and cleanup</Link>.
        </p>
      </div>
    </div>
  );
}
