import { lazy, Suspense, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Bot, Briefcase, Coins, Compass, MapPin, Server, Shield, Timer, type LucideIcon } from 'lucide-react';
import { Button } from '../../components/Button';
import { Callout } from '../../components/Callout';
import { Card } from '../../components/Card';
import { ExternalLink } from '../../components/ExternalLink';

import { SectionHeading } from '../../components/SectionHeading';
import { StageBadge } from '../../components/StageBadge';
import { FactStat } from '../../components/StatTile';
import { personas, projects, type PersonaIcon } from '../../content/data';
import { FACT_LABELS, getFacts } from '../../content/facts';
import { blob, REPO_URL } from '../../content/links';
import { PATHS, projectPath } from '../../paths';
import { STAGE_ICONS } from '../../stage';
import { CapabilityStack } from './CapabilityStack';
import { JourneyStory } from './JourneyStory';
import styles from './HomePage.module.css';

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

/** Role icons for the persona cards (always rendered aria-hidden beside the role name). */
const PERSONA_ICONS: Record<PersonaIcon, LucideIcon> = {
  server: Server,
  bot: Bot,
  shield: Shield,
  compass: Compass,
  briefcase: Briefcase,
};

export function HomePage() {
  return (
    <div className={styles.page}>
      <JourneyStory backdrop={<HeroBackdrop />} />

      {/* Project tiles: the decision matrix */}
      <section className={styles.band} aria-labelledby="tiles-heading">
        <div className="container">
          <SectionHeading
            id="tiles-heading"
            eyebrow="Projects"
            title="Pick a project"
            align="center"
            lead={
              <>
                Stage, audience, validated regions, first deploy and cost, every fact linked to its source.{' '}
                <Link to={PATHS.whichProject}>See the full comparison</Link>.
              </>
            }
          />
          <ul className={styles.tileGrid}>
            {projects.map((project) => {
              const facts = getFacts(project.id);
              const Icon = STAGE_ICONS[project.stage];
              return (
                <Card
                  as="li"
                  key={project.id}
                  variant="glow"
                  stage={project.stage}
                  interactive
                  reveal
                  padding="sm"
                  className={styles.tile}
                >
                  <div className={styles.tileHead}>
                    <span className={styles.tileIcon} aria-hidden="true">
                      <Icon size={18} />
                    </span>
                    <StageBadge stage={project.stage} label={`${project.stageNumber}. ${project.stageLabel}`} />
                  </div>
                  <h3 className={styles.tileTitle}>
                    <Link to={projectPath(project.id)}>{project.name}</Link>
                  </h3>
                  <p className={styles.tileTagline}>{project.tagline}</p>
                  <p className={styles.tileText}>
                    <strong>Best for:</strong> {project.bestFor}
                  </p>
                  <dl className={styles.facts}>
                    <FactStat
                      label={FACT_LABELS.regions}
                      fact={facts.regions}
                      project={project.shortName}
                      icon={<MapPin />}
                      noteMode="collapsed"
                    />
                    <FactStat
                      label={FACT_LABELS.firstDeploy}
                      fact={facts.firstDeploy}
                      project={project.shortName}
                      icon={<Timer />}
                      noteMode="collapsed"
                    />
                    <FactStat
                      label={FACT_LABELS.cost}
                      fact={facts.cost}
                      project={project.shortName}
                      icon={<Coins />}
                      noteMode="collapsed"
                    />
                  </dl>
                </Card>
              );
            })}
          </ul>
        </div>
      </section>

      {/* Personas */}
      <section className={`${styles.band} ${styles.bandAlt}`} aria-labelledby="personas-heading">
        <div className="container">
          <SectionHeading id="personas-heading" eyebrow="Roles" title="Who is this for?" align="center" />
          <ul className={styles.personas}>
            {personas.map((persona) => {
              const Icon = PERSONA_ICONS[persona.icon];
              return (
                <Card as="li" key={persona.role} interactive reveal corners className={styles.persona}>
                  <span className={styles.personaIcon} aria-hidden="true">
                    <Icon size={20} />
                  </span>
                  <h3 className={styles.personaRole}>{persona.role}</h3>
                  <p className={styles.personaText}>{persona.text}</p>
                  <Link to={persona.to} className={styles.personaLink} data-stretch>
                    {persona.linkLabel}
                    <ArrowRight size={16} aria-hidden="true" />
                  </Link>
                </Card>
              );
            })}
          </ul>
        </div>
      </section>

      {/* Capability stack */}
      <section className={styles.band} aria-labelledby="capabilities-heading">
        <div className="container">
          <SectionHeading
            id="capabilities-heading"
            eyebrow="Capabilities"
            title="Shared capabilities"
            align="center"
            lead="Every project draws on the same capability contracts; the implementations are replaceable."
          />
          <CapabilityStack />
        </div>
      </section>

      {/* A short pre-deploy notice (the footer carries the disclaimer). A plain div,
          not a labelled section, so the callout is not nested inside a region landmark. */}
      <div className={`${styles.band} ${styles.bandAlt} ${styles.notices}`}>
        <div className="container">
          <SectionHeading title="Before you deploy" className={styles.noticesHeading} />
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
        className={`${styles.band} ${styles.bandDark} on-dark ${styles.closing}`}
        aria-labelledby="closing-heading"
      >
        <div className="container">
          <SectionHeading id="closing-heading" title="Ready to start?" align="center" className={styles.closingHeading} />
          <div className={styles.closingActions}>
            <Button to={PATHS.start} iconEnd={<ArrowRight size={18} />}>
              Get started
            </Button>
            <Button href={REPO_URL} external variant="secondary">
              Source on GitHub
            </Button>
          </div>
        </div>
      </section>
    </div>
  );
}
