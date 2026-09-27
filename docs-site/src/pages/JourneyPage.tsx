import { Link } from 'react-router-dom';
import { ArrowRight, BookOpen, Wrench, Shield, Layers } from 'lucide-react';
import { projects } from '../content/data';
import styles from './JourneyPage.module.css';

const stageIcons = {
  learn: BookOpen,
  build: Wrench,
  govern: Shield,
  scale: Layers,
};

const tracks = [
  {
    id: 'fast',
    name: 'Fast Path',
    duration: '1-2 hours',
    description: 'Jump straight to building an agent on pre-deployed infrastructure.',
    projects: ['self-service'],
    bestFor: 'AI/ML engineers who want to build an agent quickly.',
  },
  {
    id: 'platform',
    name: 'Build the Platform',
    duration: '2-3 hours',
    description: 'Learn and build the foundation before creating agents.',
    projects: ['workshop'],
    bestFor: 'Platform engineers who need to understand the infrastructure.',
  },
  {
    id: 'governance',
    name: 'Governance Focus',
    duration: '1-2 hours',
    description: 'Implement per-tool-call authorization and security controls.',
    projects: ['mcp-gateway'],
    bestFor: 'Security engineers implementing tool governance.',
  },
  {
    id: 'enterprise',
    name: 'Enterprise Scale',
    duration: '4+ hours',
    description: 'Explore the multi-account enterprise reference blueprint.',
    projects: ['blueprint'],
    bestFor: 'Platform teams evaluating a governed multi-account foundation.',
  },
  {
    id: 'full',
    name: 'Full Journey',
    duration: '8+ hours',
    description: 'Follow the connected path from learning through fleet operations.',
    projects: ['workshop', 'self-service', 'mcp-gateway', 'blueprint'],
    bestFor: 'Solutions architects understanding the complete pattern.',
  },
];

export function JourneyPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Choose Your Path</h1>
          <p>
            Select a journey based on your role, goals, and available time.
            Each path leads to working infrastructure you can build on.
          </p>
        </div>
      </header>

      <section className={styles.stagesSection}>
        <div className={styles.sectionContent}>
          <h2>The Four Stages</h2>
          <p className={styles.sectionDescription}>
            Each project addresses a specific stage of the enterprise journey.
          </p>

          <div className={styles.stagesGrid}>
            {projects.map((project, index) => {
              const Icon = stageIcons[project.stage];
              return (
                <div
                  key={project.id}
                  className={styles.stageCard}
                  style={{ '--stage-color': project.color } as React.CSSProperties}
                >
                  <div className={styles.stageNumber}>{index + 1}</div>
                  <div className={styles.stageIcon}>
                    <Icon size={28} />
                  </div>
                  <h3 className={styles.stageName}>{project.stageLabel}</h3>
                  <p className={styles.stageProject}>{project.shortName}</p>
                  <p className={styles.stageDescription}>{project.bestFor}</p>
                  <Link to={`/projects/${project.id}`} className={styles.stageLink}>
                    Learn more <ArrowRight size={16} />
                  </Link>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section className={styles.tracksSection}>
        <div className={styles.sectionContent}>
          <h2>Recommended Tracks</h2>
          <p className={styles.sectionDescription}>
            Not sure where to start? Pick a track that matches your situation.
          </p>

          <div className={styles.tracksGrid}>
            {tracks.map((track) => (
              <div key={track.id} className={styles.trackCard}>
                <div className={styles.trackHeader}>
                  <h3 className={styles.trackName}>{track.name}</h3>
                  <span className={styles.trackDuration}>{track.duration}</span>
                </div>
                <p className={styles.trackDescription}>{track.description}</p>
                <p className={styles.trackBestFor}>
                  <strong>Best for:</strong> {track.bestFor}
                </p>
                <div className={styles.trackProjects}>
                  <span className={styles.trackProjectsLabel}>Includes:</span>
                  {track.projects.map((projectId) => {
                    const project = projects.find((p) => p.id === projectId);
                    if (!project) return null;
                    return (
                      <Link
                        key={projectId}
                        to={`/projects/${projectId}`}
                        className={styles.trackProjectBadge}
                        style={{ '--stage-color': project.color } as React.CSSProperties}
                      >
                        {project.shortName}
                      </Link>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className={styles.flexibilitySection}>
        <div className={styles.sectionContent}>
          <div className={styles.flexibilityBox}>
            <h3>Projects are Independent</h3>
            <p>
              Each project is self-contained with its own README, prerequisites, and deployment instructions.
              You can start with any project and add others later. They share architectural concepts and
              capability contracts but don't require sequential deployment.
            </p>
          </div>
        </div>
      </section>
    </div>
  );
}
