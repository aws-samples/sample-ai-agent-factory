import type { ReactNode } from 'react';
import { ArrowRight, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '../../components/Button';
import { Card } from '../../components/Card';
import { ChipNav } from '../../components/ChipNav';
import { FactsTable } from '../../components/FactsTable';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { Section } from '../../components/Section';
import { SourceLink } from '../../components/SourceLink';
import { Sources } from '../../components/Sources';
import { StageBadge } from '../../components/StageBadge';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts, type ProjectFacts, type Source } from '../../content/facts';
import { getLimitations, validated } from '../../content/limitations';
import { PATHS, projectPath } from '../../paths';
import { ReferenceNav } from './ReferenceNav';
import styles from './SupportEnvelopePage.module.css';

const ENVELOPE_SOURCE: Source = { file: 'README.md', heading: 'Support Envelope' };

/** Fact rows shown per project, in order. */
const ENVELOPE_FACT_KEYS: ReadonlyArray<keyof ProjectFacts> = ['regions', 'defaultRegion', 'status', 'version'];

const JUMP_ITEMS = projects.map((project) => ({
  label: project.shortName,
  href: `#envelope-${project.id}`,
  badge: <StageBadge stage={project.stage} label={project.stageLabel} variant="outline" />,
}));

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
        description="Validated regions, status and the known limitations each of the four Agentic AI Factory projects documents, quoted from the project READMEs with source links."
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

        <ChipNav label="Projects on this page" lead="Jump to:" items={JUMP_ITEMS} overflow="wrap" />

        {projects.map((project, index) => {
          const facts = getFacts(project.id);
          const limitations = getLimitations(project.id);
          const validatedItems = validated[project.id];
          const validatedHeading = validatedItems[0]?.source.heading ?? 'Validated reference envelope';
          return (
            <Section
              key={project.id}
              id={`envelope-${project.id}`}
              title={project.name}
              flush={index === 0}
              badge={<StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />}
              actions={
                <Button variant="ghost" size="sm" to={projectPath(project.id)} iconEnd={<ArrowRight size={16} />}>
                  Project page<span className="visually-hidden">: {project.shortName}</span>
                </Button>
              }
            >
              <FactsTable
                caption={`${project.shortName}: validated regions and status`}
                rows={ENVELOPE_FACT_KEYS.map((key) => ({ label: FACT_LABELS[key], fact: facts[key] }))}
              />

              {validatedItems.length > 0 && (
                <>
                  <h3>{validatedHeading}</h3>
                  <p className={styles.validatedIntro}>
                    What {project.shortName} states it has validated, quoted from its README (
                    <SourceLink source={validatedItems[0].source}>source</SourceLink>). Anything outside this list is
                    covered by the limitations below.
                  </p>
                  <ul className={styles.validatedList}>
                    {validatedItems.map((item) => (
                      <li key={item.id} id={item.id} className={styles.validatedItem}>
                        <ShieldCheck size={18} aria-hidden="true" className={styles.validatedIcon} />
                        <span className="visually-hidden">Validated: </span>
                        <blockquote className={styles.quote}>{renderInline(item.text)}</blockquote>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <h3>Known limitations, as documented</h3>
              <ul className={styles.limitationList}>
                {limitations.map((limitation) => (
                  <Card
                    as="li"
                    key={limitation.id}
                    id={limitation.id}
                    variant="accent"
                    stage={project.stage}
                    reveal
                    padding="sm"
                    className={styles.limitation}
                  >
                    {limitation.title && <strong className={styles.limitationTitle}>{limitation.title}</strong>}
                    <blockquote className={styles.quote}>{renderInline(limitation.text)}</blockquote>
                    <Sources sources={[limitation.source]} className={styles.limitationSource} />
                  </Card>
                ))}
              </ul>
            </Section>
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
