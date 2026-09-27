import { Link } from 'react-router-dom';
import { ArrowRight, BookOpen, Wrench, Shield, Layers } from 'lucide-react';
import { projects, capabilities } from '../content/data';
import styles from './HomePage.module.css';

const stageIcons = {
  learn: BookOpen,
  build: Wrench,
  govern: Shield,
  scale: Layers,
};

const BASE_URL = import.meta.env.BASE_URL || '/';

export function HomePage() {
  return (
    <div className={styles.page}>
      {/* Hero Section */}
      <section className={styles.hero}>
        <div className={styles.heroContent}>
          <h1 className={styles.heroTitle}>AI Agent Factory</h1>
          <p className={styles.heroSubtitle}>
            Enterprise samples for building, governing, and operating agentic AI on AWS
          </p>
          <p className={styles.heroDescription}>
            Four complementary projects that together show how to move from learning to
            agent operations at scale — with Amazon Bedrock and Amazon Bedrock AgentCore.
          </p>
          <div className={styles.heroCta}>
            <Link to="/choose-a-path" className={styles.ctaPrimary}>
              Choose Your Path <ArrowRight size={18} />
            </Link>
            <Link to="/getting-started" className={styles.ctaSecondary}>
              Getting Started
            </Link>
          </div>
        </div>
      </section>

      {/* Atlas Journey */}
      <section className={styles.atlasSection} aria-label="Atlas Journey visualization">
        <div className={styles.atlasContainer}>
          <div className={styles.atlasDesktop}>
            <img
              src={`${BASE_URL}repository-atlas-journey.svg`}
              alt="AI Agent Factory Atlas showing four projects: Workshop (Learn), Self-Service (Build), MCP Gateway (Govern), and Blueprint (Scale) connected through shared Agent Factory Capabilities"
              className={styles.atlasSvg}
              width="1200"
              height="700"
            />
          </div>
          <div className={styles.atlasMobile}>
            <h2 className={styles.atlasMobileTitle}>Your Agent Factory journey</h2>
            <p className={styles.atlasMobileDescription}>
              Four complementary starting points connected by shared capability contracts.
            </p>
            <div className={styles.atlasMobileCore}>
              <strong>Shared Agent Factory capabilities</strong>
              <small>Common architectural building blocks inherited across the journey.</small>
            </div>
            <ol className={styles.atlasMobileList}>
              {projects.map((project) => {
                const Icon = stageIcons[project.stage];
                return (
                  <li
                    key={project.id}
                    className={styles.atlasMobileItem}
                    style={{ '--stage-color': project.color } as React.CSSProperties}
                  >
                    <Link to={`/projects/${project.id}`} className={styles.atlasMobileLink}>
                      <Icon size={20} aria-hidden="true" />
                      <span>
                        <strong>{project.stageLabel}</strong>
                        <small>{project.shortName}</small>
                      </span>
                      <span className={styles.atlasMobileNumber}>{project.stageNumber}</span>
                    </Link>
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      </section>

      {/* Journey Path */}
      <section className={styles.journeySection}>
        <div className={styles.sectionContent}>
          <h2 className={styles.sectionTitle}>The Journey: Learn → Build → Govern → Scale</h2>
          <p className={styles.sectionDescription}>
            Each project addresses a different stage. Start anywhere based on your role and goals.
          </p>

          <div className={styles.projectGrid}>
            {projects.map((project) => {
              const Icon = stageIcons[project.stage];
              return (
                <Link
                  key={project.id}
                  to={`/projects/${project.id}`}
                  className={styles.projectCard}
                  style={{ '--stage-color': project.color } as React.CSSProperties}
                >
                  <div className={styles.projectHeader}>
                    <span className={styles.stageBadge}>{project.stageLabel}</span>
                    <span className={styles.stageNumber}>{project.stageNumber}</span>
                  </div>
                  <div className={styles.projectIcon}>
                    <Icon size={24} />
                  </div>
                  <h3 className={styles.projectName}>{project.shortName}</h3>
                  <p className={styles.projectDescription}>{project.description}</p>
                  <ul className={styles.projectHighlights}>
                    {project.highlights.slice(0, 3).map((highlight) => (
                      <li key={highlight}>{highlight}</li>
                    ))}
                  </ul>
                  <span className={styles.projectLink}>
                    Explore <ArrowRight size={16} />
                  </span>
                </Link>
              );
            })}
          </div>
        </div>
      </section>

      {/* Capabilities Strip */}
      <section className={styles.capabilitiesSection}>
        <div className={styles.sectionContent}>
          <h2 className={styles.sectionTitle}>Shared Capabilities</h2>
          <p className={styles.sectionDescription}>
            All projects connect to core Agent Factory capabilities. Each capability has a contract;
            the implementations shown are reference choices that can be replaced when contracts are preserved.
          </p>

          <div className={styles.capabilitiesGrid}>
            {capabilities.slice(0, 6).map((cap) => (
              <div key={cap.id} className={styles.capabilityCard}>
                <h4 className={styles.capabilityName}>{cap.name}</h4>
                <p className={styles.capabilityDescription}>{cap.description}</p>
                <div className={styles.capabilityImpl}>
                  {cap.implementations.slice(0, 2).join(' • ')}
                </div>
              </div>
            ))}
          </div>

          <Link to="/capabilities" className={styles.viewAllLink}>
            View all capabilities <ArrowRight size={16} />
          </Link>
        </div>
      </section>

      {/* Personas Section */}
      <section className={styles.personasSection}>
        <div className={styles.sectionContent}>
          <h2 className={styles.sectionTitle}>Who is this for?</h2>

          <div className={styles.personasGrid}>
            <div className={styles.personaCard}>
              <h3>Platform Engineers</h3>
              <p>Build the foundation: governed model access, tool registries, security controls, and observability.</p>
              <p className={styles.personaStart}>
                Start with <Link to="/projects/workshop">Workshop</Link> or <Link to="/projects/blueprint">Blueprint</Link>
              </p>
            </div>
            <div className={styles.personaCard}>
              <h3>AI/ML Engineers</h3>
              <p>Build agents fast using visual tools, templates, and pre-built infrastructure.</p>
              <p className={styles.personaStart}>
                Start with <Link to="/projects/self-service">Self-Service</Link>
              </p>
            </div>
            <div className={styles.personaCard}>
              <h3>Security Engineers</h3>
              <p>Govern every tool call with Cedar policies, JWT auth, and Bedrock Guardrails.</p>
              <p className={styles.personaStart}>
                Start with <Link to="/projects/mcp-gateway">MCP Gateway</Link>
              </p>
            </div>
            <div className={styles.personaCard}>
              <h3>Solutions Architects</h3>
              <p>Understand the full enterprise pattern from learning through operations at scale.</p>
              <p className={styles.personaStart}>
                Start with <Link to="/choose-a-path">Choose Your Path</Link>
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Important Notice */}
      <section className={styles.noticeSection}>
        <div className={styles.sectionContent}>
          <div className={styles.noticeBox}>
            <h3>Important</h3>
            <ul>
              <li>
                <strong>Sample code:</strong> MIT-0 license. Not an AWS service, AppSec-reviewed product,
                or compliance attestation.
              </li>
              <li>
                <strong>Real costs:</strong> All projects deploy billable AWS resources. Tear down when finished.
              </li>
              <li>
                <strong>Replaceable implementations:</strong> LiteLLM, AgentCore Gateway inference targets,
                and other components are reference choices. Replacements must preserve security, identity,
                and evidence contracts.
              </li>
              <li>
                <strong>Support envelope:</strong> Each project documents its tested regions, prerequisites,
                and known limitations in its README.
              </li>
            </ul>
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section className={styles.ctaSection}>
        <div className={styles.sectionContent}>
          <h2>Ready to start?</h2>
          <p>Pick a project that matches your goals and follow its README for deployment instructions.</p>
          <div className={styles.ctaButtons}>
            <Link to="/choose-a-path" className={styles.ctaPrimary}>
              Choose Your Path <ArrowRight size={18} />
            </Link>
            <a
              href="https://github.com/aws-samples/sample-ai-agent-factory"
              target="_blank"
              rel="noopener noreferrer"
              className={styles.ctaSecondary}
            >
              View on GitHub
            </a>
          </div>
        </div>
      </section>
    </div>
  );
}
