import { Link } from 'react-router-dom';
import { ArrowRight, ExternalLink, Terminal, AlertCircle } from 'lucide-react';
import { projects } from '../content/data';
import styles from './GettingStartedPage.module.css';

export function GettingStartedPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Getting Started</h1>
          <p>
            Quick steps to begin with any project in the AI Agent Factory repository.
          </p>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentContent}>
          <section className={styles.section}>
            <h2>1. Clone the Repository</h2>
            <div className={styles.codeBlock}>
              <code>git clone https://github.com/aws-samples/sample-ai-agent-factory.git</code>
              <code>cd sample-ai-agent-factory</code>
            </div>
          </section>

          <section className={styles.section}>
            <h2>2. Choose Your Project</h2>
            <p className={styles.sectionDescription}>
              Each project is self-contained. Pick one based on your goals:
            </p>

            <div className={styles.projectsGrid}>
              {projects.map((project) => (
                <div
                  key={project.id}
                  className={styles.projectCard}
                  style={{ '--stage-color': project.color } as React.CSSProperties}
                >
                  <span className={styles.stageBadge}>{project.stageLabel}</span>
                  <h3>{project.shortName}</h3>
                  <p>{project.bestFor}</p>
                  <div className={styles.projectFolder}>
                    <Terminal size={14} />
                    <code>cd {project.folder}</code>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className={styles.section}>
            <h2>3. Follow the Project README</h2>
            <p>
              Each project's README contains complete documentation including:
            </p>
            <ul className={styles.bulletList}>
              <li>Prerequisites and required tools</li>
              <li>Step-by-step deployment instructions</li>
              <li>Architecture overview</li>
              <li>Configuration options</li>
              <li>Cleanup/teardown commands</li>
            </ul>

            <div className={styles.readmeLinks}>
              {projects.map((project) => (
                <a
                  key={project.id}
                  href={`https://github.com/aws-samples/sample-ai-agent-factory/blob/main/${project.folder}/README.md`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.readmeLink}
                  style={{ '--stage-color': project.color } as React.CSSProperties}
                >
                  {project.shortName} README <ExternalLink size={14} />
                </a>
              ))}
            </div>
          </section>

          <section className={styles.section}>
            <h2>Common Prerequisites</h2>
            <p className={styles.sectionDescription}>
              Most projects require these tools. Check each project's README for specifics.
            </p>

            <div className={styles.prereqsGrid}>
              <div className={styles.prereqCard}>
                <h4>AWS CLI v2</h4>
                <p>Configured with credentials for the target account</p>
                <a href="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" target="_blank" rel="noopener noreferrer">
                  Install Guide <ExternalLink size={12} />
                </a>
              </div>
              <div className={styles.prereqCard}>
                <h4>Node.js 20+</h4>
                <p>Required for CDK and TypeScript projects</p>
                <a href="https://nodejs.org/" target="_blank" rel="noopener noreferrer">
                  Download <ExternalLink size={12} />
                </a>
              </div>
              <div className={styles.prereqCard}>
                <h4>Python 3.12+</h4>
                <p>Required for Lambda functions and Python CDK</p>
                <a href="https://www.python.org/downloads/" target="_blank" rel="noopener noreferrer">
                  Download <ExternalLink size={12} />
                </a>
              </div>
              <div className={styles.prereqCard}>
                <h4>AWS CDK</h4>
                <p>Infrastructure as code (often invoked via npx)</p>
                <a href="https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html" target="_blank" rel="noopener noreferrer">
                  Getting Started <ExternalLink size={12} />
                </a>
              </div>
            </div>
          </section>

          <section className={styles.warningSection}>
            <div className={styles.warningBox}>
              <AlertCircle size={24} />
              <div>
                <h3>Cost Notice</h3>
                <p>
                  All projects deploy real, billable AWS resources including ECS Fargate,
                  DocumentDB, Aurora, NAT Gateways, Load Balancers, Lambda functions, and
                  Amazon Bedrock model invocations.
                </p>
                <p>
                  <strong>Tear down resources when finished</strong> using the cleanup commands
                  documented in each project to stop charges.
                </p>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Local Development</h2>
            <p>To run this documentation site locally:</p>
            <div className={styles.codeBlock}>
              <code>cd docs-site</code>
              <code>npm ci</code>
              <code>npm run dev</code>
            </div>
            <p className={styles.codeNote}>
              The site runs at <code>http://127.0.0.1:5173/sample-ai-agent-factory/</code>
            </p>
          </section>

          <section className={styles.ctaSection}>
            <h2>Not sure where to start?</h2>
            <p>Use the journey selector to find the right path for your goals.</p>
            <Link to="/choose-a-path" className={styles.ctaPrimary}>
              Choose Your Path <ArrowRight size={18} />
            </Link>
          </section>
        </div>
      </div>
    </div>
  );
}
