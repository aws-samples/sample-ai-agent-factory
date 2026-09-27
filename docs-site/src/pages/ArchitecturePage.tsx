import { ExternalLink } from 'lucide-react';
import styles from './ArchitecturePage.module.css';

const BASE_URL = import.meta.env.BASE_URL || '/';

export function ArchitecturePage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Architecture</h1>
          <p>
            Repository architecture overview and links to detailed project diagrams.
          </p>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentInner}>
          <section className={styles.section}>
            <h2>Repository Atlas</h2>
            <p className={styles.sectionDescription}>
              This diagram shows how the four projects relate to each other and to
              the shared Agent Factory capabilities at the center.
            </p>
            <div className={styles.atlasContainer}>
              <img
                src={`${BASE_URL}repository-atlas-journey.svg`}
                alt="AI Agent Factory Atlas showing four projects: Workshop (Learn), Self-Service (Build), MCP Gateway (Govern), and Blueprint (Scale) connected through shared Agent Factory Capabilities"
                className={styles.atlasSvg}
                width="1200"
                height="700"
              />
              <a
                href={`${BASE_URL}repository-atlas-journey.svg`}
                target="_blank"
                rel="noopener noreferrer"
                className={styles.diagramLink}
              >
                Open full-size Atlas <ExternalLink size={14} aria-hidden="true" />
              </a>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Enterprise Blueprint Architecture</h2>
            <p className={styles.sectionDescription}>
              The Enterprise Blueprint includes detailed architecture diagrams showing
              both the operating model view and the AWS service-level implementation.
            </p>

            <div className={styles.diagramsGrid}>
              <div className={styles.diagramCard}>
                <h3>Operating Model View</h3>
                <p>
                  Shows people, ownership, shared capability planes, repeatable cells,
                  and governed flows. AWS labels illustrate the reference choices; the
                  capability boundaries are the architecture.
                </p>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-concept.svg"
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.diagramLink}
                >
                  View Diagram <ExternalLink size={14} />
                </a>
              </div>

              <div className={styles.diagramCard}>
                <h3>AWS Services View</h3>
                <p>
                  The concrete services deployed by the blueprint and the numbered
                  release/run cycle used by its live evidence.
                </p>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-aws-services.svg"
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.diagramLink}
                >
                  View Diagram <ExternalLink size={14} />
                </a>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Key Architectural Concepts</h2>

            <div className={styles.conceptsGrid}>
              <article className={styles.conceptCard}>
                <h3>Capability Contracts</h3>
                <p>
                  The architecture standardizes what each component must do, not one
                  product for every customer. Implementations are replaceable when
                  contracts are preserved.
                </p>
              </article>

              <article className={styles.conceptCard}>
                <h3>Four Planes</h3>
                <p>
                  Developer experience plane, Platform control plane, Workstream execution
                  plane, and Assurance/operations plane — each with distinct ownership.
                </p>
              </article>

              <article className={styles.conceptCard}>
                <h3>Workstream Cells</h3>
                <p>
                  Isolated application execution boundaries containing team-owned Runtime,
                  Memory, Tool Gateway, tools, and application data.
                </p>
              </article>

              <article className={styles.conceptCard}>
                <h3>Governed Flows</h3>
                <p>
                  Software delivery flow, Inference flow, Tool flow, and Observability
                  flow — each with explicit contracts and controls.
                </p>
              </article>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Project-Specific Documentation</h2>
            <p className={styles.sectionDescription}>
              Each project's README contains detailed architecture information specific
              to that deployment pattern.
            </p>

            <div className={styles.docsGrid}>
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/workshop-building-agentic-ai-platform/README.md"
                target="_blank"
                rel="noopener noreferrer"
                className={styles.docLink}
              >
                Workshop Architecture <ExternalLink size={14} />
              </a>
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/Agentic-ai-self-service/README.md"
                target="_blank"
                rel="noopener noreferrer"
                className={styles.docLink}
              >
                Self-Service Architecture <ExternalLink size={14} />
              </a>
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-mcp-governance-gateway/README.md"
                target="_blank"
                rel="noopener noreferrer"
                className={styles.docLink}
              >
                MCP Gateway Architecture <ExternalLink size={14} />
              </a>
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/README.md"
                target="_blank"
                rel="noopener noreferrer"
                className={styles.docLink}
              >
                Blueprint Architecture <ExternalLink size={14} />
              </a>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
