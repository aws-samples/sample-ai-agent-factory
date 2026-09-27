import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import styles from './HowItWorksPage.module.css';

export function HowItWorksPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>How It Works</h1>
          <p>
            Understanding the relationship between projects and
            how they combine to form an AI Agent Factory.
          </p>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentContent}>
          <section className={styles.section}>
            <h2>The Agent Factory Concept</h2>
            <p>
              An <strong>AI Agent Factory</strong> is the people, patterns, and platform that let an
              organization turn ideas into production agents reliably and at scale. The hard part is
              not creating the first agent — it's everything around the model: governed access,
              reusable tools, security and authorization, observability, and a repeatable way to ship
              agents to production.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Three Parts of the Story</h2>
            <div className={styles.partsGrid}>
              <div className={styles.partCard}>
                <span className={styles.partNumber}>1</span>
                <h3>Platform Foundation</h3>
                <p>
                  The governed, enterprise-grade landing zone an organization stands up once so
                  every team can build on shared, secured infrastructure.
                </p>
                <p className={styles.partProjects}>
                  <strong>Projects:</strong> Workshop, Blueprint
                </p>
              </div>
              <div className={styles.partCard}>
                <span className={styles.partNumber}>2</span>
                <h3>Builder Experience</h3>
                <p>
                  The low-code/no-code surface that lets engineers (and non-engineers) design,
                  deploy, and operate agents on top of that foundation.
                </p>
                <p className={styles.partProjects}>
                  <strong>Projects:</strong> Self-Service
                </p>
              </div>
              <div className={styles.partCard}>
                <span className={styles.partNumber}>3</span>
                <h3>Governed Front Door</h3>
                <p>
                  The enforcement layer that decides, for each individual tool call, whether an
                  agent may reach a given internal tool or SaaS system.
                </p>
                <p className={styles.partProjects}>
                  <strong>Projects:</strong> MCP Gateway
                </p>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Capability Contracts vs Implementations</h2>
            <p>
              The architecture standardizes <strong>what each component must do</strong>, not one
              product for every customer. Labels like LLM Gateway, Tool Gateway, agent runtime,
              memory, identity, registry, policy engine, delivery pipeline, and observability
              describe architectural capabilities.
            </p>
            <div className={styles.contractBox}>
              <h4>Reference Implementations</h4>
              <p>
                AgentCore Gateway inference targets, LiteLLM, AgentCore Runtime, AgentCore Memory,
                AgentCore Identity, AWS Agent Registry, CodePipeline, and CloudWatch are
                <em> implementation choices</em> in this repository.
              </p>
              <p>
                Customers can select alternatives that fit their standards, but each replacement
                must preserve the stated security, identity, tenancy, lifecycle, and evidence
                contracts.
              </p>
            </div>
          </section>

          <section className={styles.section}>
            <h2>How an Engineer Ships an Agent</h2>
            <ol className={styles.stepsList}>
              <li>
                <strong>Discover a paved road</strong> — Select a task, chatbot, or multi-agent
                template maintained by the platform team.
              </li>
              <li>
                <strong>Create in a team-owned repository</strong> — The template supplies
                approved Gateway clients, prompt and tool extension points, evaluation corpus, and
                metadata contract.
              </li>
              <li>
                <strong>Select governed capabilities</strong> — Models come from the Platform
                allow-list. Tools come from approved Registry records. Guardrail profiles come from
                the supported catalog.
              </li>
              <li>
                <strong>Open a pull request</strong> — Source review, static checks, manifest
                hashing, image scanning, policy validation, and evaluation run before deployment.
              </li>
              <li>
                <strong>Deploy to the Workstream cell</strong> — The pipeline creates stable roles,
                completes the Platform permission handoff, and deploys nonproduction resources.
              </li>
              <li>
                <strong>Prove behavior</strong> — The deployed Runtime is exercised with authorized
                and adversarial twins. Quality, tool success, refusal, latency, cost, rollback, and
                telemetry gates fail closed.
              </li>
              <li>
                <strong>Approve production</strong> — A human reviews evidence rather than a
                generic "pipeline succeeded" signal.
              </li>
              <li>
                <strong>Operate with the fleet</strong> — The team owns its application SLOs and
                data; the platform team owns shared service health and common controls.
              </li>
            </ol>
          </section>

          <section className={styles.section}>
            <h2>Project Independence</h2>
            <p>
              Each project in this repository is self-contained. You can deploy any project
              independently based on your needs:
            </p>
            <ul className={styles.bulletList}>
              <li>
                <strong>Workshop</strong> teaches the patterns in a single account
              </li>
              <li>
                <strong>Self-Service</strong> provides a visual builder without requiring the
                full enterprise stack
              </li>
              <li>
                <strong>MCP Gateway</strong> adds governance to any existing tool infrastructure
              </li>
              <li>
                <strong>Blueprint</strong> is the multi-account, SCP-governed, CI/CD-gated form
                of the platform
              </li>
            </ul>
          </section>

          <section className={styles.ctaSection}>
            <h2>Ready to explore?</h2>
            <div className={styles.ctaButtons}>
              <Link to="/choose-a-path" className={styles.ctaPrimary}>
                Choose Your Path <ArrowRight size={18} />
              </Link>
              <Link to="/capabilities" className={styles.ctaSecondary}>
                View Capabilities
              </Link>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
