import { ExternalLink, AlertTriangle, Shield, Lock, Eye } from 'lucide-react';
import styles from './SecurityPage.module.css';

export function SecurityPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Security</h1>
          <p>
            Security considerations, important notices, and links to
            project-specific security documentation.
          </p>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentContent}>
          <section className={styles.warningSection}>
            <div className={styles.warningBox}>
              <AlertTriangle size={24} />
              <div>
                <h2>Important Notice</h2>
                <p>
                  This is <strong>sample code</strong> provided under MIT-0 License. It is not an
                  AWS service, an AppSec-reviewed product, a compliance attestation, or proof of
                  load at a particular organizational size.
                </p>
                <p>
                  Review the architecture, IAM policies, quotas, data handling, operating model,
                  and costs before using it with production or regulated workloads.
                </p>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Security Features by Project</h2>

            <div className={styles.projectsGrid}>
              <article className={styles.projectCard}>
                <h3>Workshop</h3>
                <ul>
                  <li>Guided learning environment</li>
                  <li>Scoped IAM policies for event deployment</li>
                  <li>Validated regions for model access</li>
                  <li>Preflight capability checks</li>
                </ul>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/workshop-building-agentic-ai-platform/README.md#prerequisites-self-paced"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View Prerequisites <ExternalLink size={14} />
                </a>
              </article>

              <article className={styles.projectCard}>
                <h3>Self-Service</h3>
                <ul>
                  <li>Cognito authentication</li>
                  <li>Cedar policy enforcement</li>
                  <li>Secrets Manager for credentials</li>
                  <li>WAF protection on CloudFront</li>
                </ul>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/Agentic-ai-self-service/docs/SECURITY_HARDENING.md"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View Security Hardening <ExternalLink size={14} />
                </a>
              </article>

              <article className={styles.projectCard}>
                <h3>MCP Gateway</h3>
                <ul>
                  <li>JWT authentication (Cognito)</li>
                  <li>Cedar policy evaluation (ENFORCE mode)</li>
                  <li>Bedrock Guardrail screening</li>
                  <li>Request/response interceptors</li>
                </ul>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-mcp-governance-gateway/README.md#verified-architecture"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View Architecture <ExternalLink size={14} />
                </a>
              </article>

              <article className={styles.projectCard}>
                <h3>Enterprise Blueprint</h3>
                <ul>
                  <li>AWS Organizations and service control policies</li>
                  <li>PrivateLink-only egress</li>
                  <li>Customer-managed KMS keys</li>
                  <li>Mandatory evaluation gates</li>
                </ul>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/enterprise-agentic-ai-platform-blueprint/README.md#10-security"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View Security Section <ExternalLink size={14} />
                </a>
              </article>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Security Principles</h2>

            <div className={styles.principlesGrid}>
              <div className={styles.principleCard}>
                <Shield size={24} />
                <h3>Least Privilege</h3>
                <p>
                  IAM policies are scoped to required actions. Each project documents
                  its permission requirements explicitly.
                </p>
              </div>

              <div className={styles.principleCard}>
                <Lock size={24} />
                <h3>Encryption</h3>
                <p>
                  Data at rest and in transit is encrypted. The Blueprint uses
                  customer-managed KMS keys with documented grant operations.
                </p>
              </div>

              <div className={styles.principleCard}>
                <Eye size={24} />
                <h3>Observability</h3>
                <p>
                  CloudWatch logs, metrics, and traces provide visibility.
                  Audit trails capture access and changes.
                </p>
              </div>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Before Production Use</h2>

            <div className={styles.checklistBox}>
              <p>Before deploying to production environments, review:</p>
              <ul>
                <li>IAM policies and trust relationships in each stack</li>
                <li>Network configuration and egress patterns</li>
                <li>Data classification and retention requirements</li>
                <li>Compliance obligations for your organization</li>
                <li>Cost projections and budget alerts</li>
                <li>Backup and disaster recovery plans</li>
                <li>Incident response procedures</li>
                <li>Known limitations documented in each project README</li>
              </ul>
            </div>
          </section>

          <section className={styles.section}>
            <h2>Reporting Security Issues</h2>
            <p>
              See{' '}
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CONTRIBUTING.md#security-issue-notifications"
                target="_blank"
                rel="noopener noreferrer"
              >
                CONTRIBUTING.md
              </a>{' '}
              for information on reporting security issues.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}
