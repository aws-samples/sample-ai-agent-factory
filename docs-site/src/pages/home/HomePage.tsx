import { lazy, Suspense, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { Callout } from '../../components/Callout';
import { ExternalLink } from '../../components/ExternalLink';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { StageBadge } from '../../components/StageBadge';
import { capabilities, projects } from '../../content/data';
import { FACT_LABELS, getAdvisories, getFacts } from '../../content/facts';
import { blob, REPO_URL } from '../../content/links';
import { PATHS, projectPath } from '../../paths';
import { FactItem } from '../start/shared';
import shared from '../start/start.module.css';
import styles from './HomePage.module.css';

const BASE_URL = import.meta.env.BASE_URL || '/';

const Constellation = lazy(() => import('../../components/Constellation'));

/**
 * Hero backdrop: mounts the constellation only on the client, after the first paint (idle callback,
 * or 150ms where requestIdleCallback is missing), so its chunk never delays the largest contentful
 * paint and the prerendered HTML contains just the empty backdrop wrapper.
 */
function HeroBackdrop() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (typeof window.requestIdleCallback === 'function') {
      const id = window.requestIdleCallback(() => setReady(true));
      return () => window.cancelIdleCallback(id);
    }
    const timer = window.setTimeout(() => setReady(true), 150);
    return () => window.clearTimeout(timer);
  }, []);

  if (!ready) return null;
  return (
    <Suspense fallback={null}>
      <Constellation />
    </Suspense>
  );
}

/** Same wording as the atlas figure on the Architecture page; project names match the SVG labels. */
const ATLAS_ALT =
  'AI Agent Factory Atlas: four complementary projects for enterprise agentic AI on AWS. In the centre sits a hub labelled Agent Factory Capabilities: LLM Gateway, Tool Gateway, Identity and Registry, Policy and Observability. Around it, four stages in order. Learn: Workshop, hands-on platform patterns. Build: Visual Workflow Platform, drag-and-drop agent builder. Govern: MCP Governance Gateway, per-tool-call authorization. Scale: Enterprise Blueprint, multi-account reference blueprint. A legend distinguishes the sequential journey path, a skip-ahead path, and the shared-capability links from each project to the hub. A footer reads: Each project is self-contained. Start anywhere based on your role and goals.';

interface Persona {
  role: string;
  text: string;
  startLabel: string;
  to: string;
}

const personas: Persona[] = [
  {
    role: 'Platform engineer',
    text: 'Build the LLM Gateway, registries and Tools Gateway module by module.',
    startLabel: 'Start with the Workshop',
    to: projectPath('workshop'),
  },
  {
    role: 'AI/ML engineer',
    text: 'Ship an agent on Amazon Bedrock AgentCore from a template on a visual canvas.',
    startLabel: 'Start with Self-Service',
    to: projectPath('self-service'),
  },
  {
    role: 'Security engineer',
    text: 'See Cedar ENFORCE, JWT authentication, interceptors and a Guardrail on a live MCP endpoint.',
    startLabel: 'Start with the MCP Gateway',
    to: projectPath('mcp-gateway'),
  },
  {
    role: 'Solutions architect',
    text: 'Compare regions, deploy time, cost and topology before recommending a project.',
    startLabel: 'Start with the comparison',
    to: PATHS.whichProject,
  },
  {
    role: 'Engineering director',
    text: 'Know what each sample is and is not before committing a team.',
    startLabel: 'Start with the support envelope',
    to: PATHS.referenceSupportEnvelope,
  },
];

export function HomePage() {
  const openAdvisories = projects.flatMap((project) =>
    getAdvisories(project.id).map((advisory) => ({ project, advisory })),
  );

  return (
    <div className={styles.page}>
      <PageMeta
        title="AI Agent Factory: enterprise agentic AI samples on AWS"
        description="Four complementary AWS samples for learning, building, governing and scaling agentic AI with Amazon Bedrock and Amazon Bedrock AgentCore, compared by stage, regions, deploy time and cost."
        titleIsFull
      />

      <PageHeader
        title="AI Agent Factory"
        lead="Enterprise samples for building, governing, and operating agentic AI on AWS with Amazon Bedrock and Amazon Bedrock AgentCore."
        backdrop={<HeroBackdrop />}
        actions={
          <>
            <Link to={PATHS.whichProject} className={shared.btnPrimary}>
              Find your project
              <ArrowRight size={18} aria-hidden="true" />
            </Link>
            <Link to={PATHS.start} className={shared.btnSecondary}>
              Get started
            </Link>
          </>
        }
      />

      {/* Atlas: image from 1024 px, typed list below */}
      <section className={`${shared.band} ${shared.bandDark} on-dark ${styles.atlas}`} aria-labelledby="atlas-heading">
        <div className="container">
          <h2 id="atlas-heading" className={styles.atlasHeading}>
            One journey, four starting points
          </h2>
          <img
            className={styles.atlasImage}
            src={`${BASE_URL}repository-atlas-journey.svg`}
            alt={ATLAS_ALT}
            width="1200"
            height="700"
          />
          <ol className={styles.stageList}>
            {projects.map((project) => (
              <li key={project.id}>
                <Link to={projectPath(project.id)} className={styles.stageLink}>
                  <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                  <span className={styles.stageText}>
                    <strong>{project.shortName}</strong>
                    <span className={styles.stageTagline}>{project.tagline}</span>
                  </span>
                  <ArrowRight size={18} aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* Project tiles: the decision matrix */}
      <section className={shared.band} aria-labelledby="tiles-heading">
        <div className="container">
          <div className={shared.sectionHead}>
            <h2 id="tiles-heading">Pick a project</h2>
            <p className={shared.lead}>
              Stage, audience, validated regions, first deploy and cost, every fact linked to its source.{' '}
              <Link to={PATHS.whichProject}>See the full comparison</Link>.
            </p>
          </div>
          <ul className={`${shared.tileGrid} ${shared.tileGrid4}`}>
            {projects.map((project) => {
              const facts = getFacts(project.id);
              return (
                <li key={project.id} className={shared.tile} data-stage={project.stage} data-reveal data-lift>
                  <div className={shared.tileHead}>
                    <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                    <Link to={projectPath(project.id)} className={shared.tileLink}>
                      Details
                      <span className="visually-hidden"> about {project.shortName}</span>
                      <ArrowRight size={16} aria-hidden="true" />
                    </Link>
                  </div>
                  <h3 className={shared.tileTitle}>{project.name}</h3>
                  <p className={shared.tileTagline}>{project.tagline}</p>
                  <p className={shared.tileText}>
                    <strong>Best for:</strong> {project.bestFor}
                  </p>
                  <dl className={shared.facts}>
                    <FactItem label={FACT_LABELS.regions} fact={facts.regions} project={project.shortName} compactNote />
                    <FactItem label={FACT_LABELS.firstDeploy} fact={facts.firstDeploy} project={project.shortName} compactNote />
                    <FactItem label={FACT_LABELS.cost} fact={facts.cost} project={project.shortName} compactNote />
                  </dl>
                </li>
              );
            })}
          </ul>
        </div>
      </section>

      {/* Personas */}
      <section className={`${shared.band} ${shared.bandAlt}`} aria-labelledby="personas-heading">
        <div className="container">
          <div className={shared.sectionHead}>
            <h2 id="personas-heading">Who is this for?</h2>
          </div>
          <ul className={styles.personas}>
            {personas.map((persona) => (
              <li key={persona.role} className={styles.persona} data-reveal>
                <h3 className={styles.personaRole}>{persona.role}</h3>
                <p>
                  {persona.text}{' '}
                  <Link to={persona.to} className={styles.personaLink}>
                    {persona.startLabel}
                    <ArrowRight size={14} aria-hidden="true" />
                  </Link>
                </p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* Capability strip */}
      <section className={shared.band} aria-labelledby="capabilities-heading">
        <div className="container">
          <div className={shared.sectionHead}>
            <h2 id="capabilities-heading">Shared capabilities</h2>
            <p className={shared.lead}>
              Every project draws on the same capability contracts; the implementations are replaceable.
            </p>
          </div>
          <ul className={shared.chips}>
            {capabilities.slice(0, 6).map((capability) => (
              <li key={capability.id}>
                <Link to={PATHS.conceptsCapabilityContracts} className={shared.chip}>
                  {capability.name}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* Advisories and a short pre-deploy notice (the footer carries the disclaimer). A plain div,
          not a labelled section, so the callouts are not nested inside a region landmark. */}
      <div className={`${shared.band} ${shared.bandAlt} ${styles.notices}`}>
        <div className="container">
          <h2 className={styles.noticesHeading}>Before you deploy</h2>
          {openAdvisories.length > 0 && (
            <div data-reveal>
              <Callout kind="warning" title="Open advisories">
                <ul className={styles.advisoryList}>
                  {openAdvisories.map(({ project, advisory }) => (
                    <li key={advisory.id}>
                      <strong>{project.shortName}:</strong>{' '}
                      <ExternalLink href={advisory.url}>
                        Issue #{advisory.issue}: {advisory.title}
                      </ExternalLink>
                    </li>
                  ))}
                </ul>
                <p>
                  <Link to={PATHS.referenceSupportEnvelope}>What each advisory means for you</Link>
                </p>
              </Callout>
            </div>
          )}
          <div data-reveal>
            <Callout kind="important" title="Real resources, real costs">
              <p>
                Every project deploys real, billable AWS resources. Check the cost notes and teardown steps, the support
                envelope, and the license before you deploy.
              </p>
              <ul className={styles.noticeLinks}>
                <li>
                  <Link to={PATHS.costsAndCleanup}>Costs and cleanup</Link>
                </li>
                <li>
                  <Link to={PATHS.referenceSupportEnvelope}>Support envelope</Link>
                </li>
                <li>
                  <ExternalLink href={blob('LICENSE')}>License</ExternalLink>
                </li>
              </ul>
            </Callout>
          </div>
        </div>
      </div>

      {/* Closing call to action */}
      <section
        className={`${shared.band} ${shared.bandDark} on-dark ${styles.closing}`}
        aria-labelledby="closing-heading"
      >
        <div className="container">
          <h2 id="closing-heading">Ready to start?</h2>
          <div className={`${shared.actions} ${styles.closingActions}`}>
            <Link to={PATHS.start} className={shared.btnPrimary}>
              Get started
              <ArrowRight size={18} aria-hidden="true" />
            </Link>
            <ExternalLink href={REPO_URL} className={shared.btnSecondary}>
              Source on GitHub
            </ExternalLink>
          </div>
        </div>
      </section>
    </div>
  );
}
