import { Link } from 'react-router-dom';
import { ExternalLink } from '../../components/ExternalLink';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ReferenceNav } from './ReferenceNav';
import { StageBadge } from '../../components/StageBadge';
import { getProjectById, type ProjectId } from '../../content/data';
import { docById, PROJECT_FOLDERS, type DocEntry } from '../../content/docs';
import { blob, githubHeadingSlug, ISSUES_URL, VULN_REPORT_URL } from '../../content/links';
import { securityMatrix } from '../../content/matrix';
import { docLabel } from '../../docs/docModules';
import { PATHS, projectPath, projectReadmePath } from '../../paths';
import { PostureLegend, PostureMatrix } from '../concepts/PostureMatrix';
import styles from './SecurityPage.module.css';

const PRODUCTION_CHECKLIST = [
  'IAM policies and trust relationships in each stack',
  'Network configuration and egress patterns',
  'Data classification and retention requirements',
  'Compliance obligations for your organization',
  'Cost projections and budget alerts',
  'Backup and disaster recovery plans',
  'Incident response procedures',
];

/** A README section that is rendered on this site and also readable on GitHub. */
interface ReadmeSection {
  heading: string;
  label: string;
}

/** A repository file that is not rendered on the site; linked on GitHub only. */
interface GitHubOnlyDoc {
  path: string;
  label: string;
  why: string;
}

interface ProjectSecurityDocs {
  projectId: ProjectId;
  /** Rendered site docs (from docs.ts), by id. */
  siteDocIds: string[];
  /** README sections worth reading first. */
  readmeSections: ReadmeSection[];
  /** Files only available on GitHub. */
  githubOnly: GitHubOnlyDoc[];
}

const SECURITY_DOCS: ProjectSecurityDocs[] = [
  {
    projectId: 'workshop',
    siteDocIds: [],
    readmeSections: [{ heading: 'Prerequisites (self-paced)', label: 'Scoped deploy policies and account guidance' }],
    githubOnly: [
      {
        path: `${PROJECT_FOLDERS.workshop}/static/cfn/POLICY_NOTES.md`,
        label: 'ParticipantRole IAM policy notes (POLICY_NOTES.md)',
        why: 'Size budget and scoping conventions for the participant and deploy policies. Not rendered on this site.',
      },
    ],
  },
  {
    projectId: 'self-service',
    siteDocIds: [
      'self-service/docs/security-hardening',
      'self-service/docs/data-retention',
      'self-service/docs/rbac-rollout',
      'self-service/docs/registry-and-rbac',
      'self-service/docs/personas',
    ],
    readmeSections: [{ heading: 'Deploying to another region', label: 'WAF scope outside us-east-1' }],
    githubOnly: [],
  },
  {
    projectId: 'mcp-gateway',
    siteDocIds: ['mcp-gateway/connectors/atlassian'],
    readmeSections: [
      { heading: 'Security notes', label: 'Security notes' },
      { heading: 'Tracked production hardening (not in this sample)', label: 'Tracked production hardening' },
    ],
    githubOnly: [],
  },
  {
    projectId: 'blueprint',
    siteDocIds: [],
    readmeSections: [
      { heading: '10. Security', label: 'Section 10, Security' },
      { heading: '15. Known limitations and support envelope', label: 'Section 15, Known limitations and support envelope' },
    ],
    githubOnly: [],
  },
];

function readmeFile(projectId: ProjectId): string {
  return `${PROJECT_FOLDERS[projectId]}/README.md`;
}

export function SecurityPage() {
  const matrixRows = securityMatrix.map((row) => ({ id: row.id, name: row.name, cells: row.cells }));

  return (
    <div className={styles.page}>
      <PageMeta
        title="Security"
        description="Security controls by project across authentication, authorization, encryption, network, audit and guardrails, what to review before production use, where each project's security documentation lives, and how to report a vulnerability."
      />
      <PageHeader
        eyebrow="Reference"
        title="Security"
        lead="Security controls by project for four samples centered on Amazon Bedrock and Amazon Bedrock AgentCore: what to review before production, where each project's security documentation lives, and how to report a vulnerability."
      />

      <div className="container">
        <ReferenceNav />

        <section className={styles.section} aria-labelledby="controls">
          <h2 id="controls">Security controls by project</h2>
          <p className={styles.prose}>
            Each cell states the control as the project ships it, with a link to the file that says so. Postures are
            coarse on purpose; read the cell text before relying on a control.
          </p>
          <PostureLegend />
          <PostureMatrix caption="Security control posture by project, with sources" rowHeader="Control" rows={matrixRows} />
        </section>

        <section className={styles.section} aria-labelledby="before-production">
          <h2 id="before-production">Before production use</h2>
          <div className={styles.checklistBox}>
            <p>Before deploying any of these projects to a production environment, review:</p>
            <ul>
              {PRODUCTION_CHECKLIST.map((item) => (
                <li key={item}>{item}</li>
              ))}
              <li>
                Known limitations documented in each project README, collected on the{' '}
                <Link to={PATHS.referenceSupportEnvelope}>support envelope</Link> page
              </li>
            </ul>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="docs">
          <h2 id="docs">Security documentation on this site</h2>
          <p className={styles.prose}>
            Rendered documents open on this site. README sections open on the rendered README page, with a GitHub link
            beside each. Files the site does not render link straight to GitHub.
          </p>
          <div className={styles.docGrid}>
            {SECURITY_DOCS.map((entry) => {
              const project = getProjectById(entry.projectId);
              if (!project) return null;
              const siteDocs = entry.siteDocIds.map((id) => docById(id)).filter((d): d is DocEntry => d !== undefined);
              return (
                <article key={entry.projectId} className={styles.docCard}>
                  <div className={styles.docHeading}>
                    <StageBadge stage={project.stage} label={project.stageLabel} />
                    <h3>
                      <Link to={projectPath(project.id)}>{project.shortName}</Link>
                    </h3>
                  </div>
                  <ul className={styles.docList}>
                    <li>
                      <Link to={projectReadmePath(project.id)}>README on this site</Link>
                    </li>
                    {siteDocs.map((doc) => (
                      <li key={doc.id}>
                        <Link to={doc.route}>{docLabel(doc)}</Link>
                      </li>
                    ))}
                    {entry.readmeSections.map((section) => {
                      const slug = githubHeadingSlug(section.heading);
                      return (
                        <li key={section.heading}>
                          <Link to={`${projectReadmePath(project.id)}#${slug}`}>README: {section.label}</Link>{' '}
                          <span className={styles.docAside}>
                            (<ExternalLink href={blob(readmeFile(project.id), slug)}>on GitHub</ExternalLink>)
                          </span>
                        </li>
                      );
                    })}
                    {entry.githubOnly.map((doc) => (
                      <li key={doc.path}>
                        <ExternalLink href={blob(doc.path)}>{doc.label}</ExternalLink>
                        <span className={styles.docAside}> {doc.why}</span>
                      </li>
                    ))}
                  </ul>
                </article>
              );
            })}
          </div>
        </section>

        <section className={styles.section} aria-labelledby="reporting">
          <h2 id="reporting">Reporting</h2>
          <div className={styles.reportingBox}>
            <p>
              <strong>Security vulnerabilities:</strong> do not open a public GitHub issue. Report them through the{' '}
              <ExternalLink href={VULN_REPORT_URL}>AWS vulnerability reporting page</ExternalLink>, as the repository&apos;s{' '}
              <Link to={`${PATHS.contributing}#security-issue-notifications`}>contributing guidelines</Link> ask.
            </p>
            <p>
              <strong>Bugs and documentation problems:</strong> open an issue in the{' '}
              <ExternalLink href={ISSUES_URL}>GitHub issue tracker</ExternalLink>.
            </p>
          </div>
        </section>
      </div>
    </div>
  );
}
