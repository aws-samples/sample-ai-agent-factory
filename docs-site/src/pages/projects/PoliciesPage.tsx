import { Link } from 'react-router-dom';
import { cedarPolicies } from 'virtual:repo-index';
import { Callout } from '../../components/Callout';
import { CodeBlock } from '../../components/CodeBlock';
import { ExternalLink } from '../../components/ExternalLink';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { getProjectById } from '../../content/data';
import { tree } from '../../content/links';
import {
  disabledPoliciesNote,
  GATEWAY_POLICIES_DIR,
  manifestValidationNote,
  policyPurposeFor,
} from '../../content/projects/mcp-gateway-demo';
import { projectPath } from '../../paths';
import { Sources } from './sections/shared';
import sectionStyles from './sections/ProjectSections.module.css';
import detailStyles from './ProjectDetailPage.module.css';
import styles from './PoliciesPage.module.css';

export function PoliciesPage() {
  const project = getProjectById('mcp-gateway');
  const active = cedarPolicies.filter((policy) => policyPurposeFor(policy.name)?.active !== false);
  const disabled = cedarPolicies.filter((policy) => policyPurposeFor(policy.name)?.active === false);

  return (
    <div className={styles.page}>
      <PageMeta
        title="Cedar policies"
        description="Every Cedar policy file shipped with the Enterprise MCP Governance Gateway, rendered from the repository with a one-line purpose each and the manifest's validation mode."
      />
      <PageHeader
        eyebrow={project?.name ?? 'Enterprise MCP Governance Gateway'}
        stage="govern"
        stageLabel={project?.stageLabel ?? 'Govern'}
        title="Cedar policies"
        lead={`The ${cedarPolicies.length} policy files under policies/, rendered from the repository. ${active.length} are deployed by the manifest and ${disabled.length} are kept disabled.`}
        actions={
          <>
            <Link to={projectPath('mcp-gateway')} className={detailStyles.actionPrimary}>
              Back to the project page
            </Link>
            <ExternalLink href={tree(GATEWAY_POLICIES_DIR)} className={detailStyles.actionSecondary}>
              policies/ on GitHub
            </ExternalLink>
          </>
        }
      />

      <div className="container">
        <div className={styles.body}>
          <div className={sectionStyles.callouts}>
            <Callout kind="important" title="Validation mode">
              <p>{manifestValidationNote.text}</p>
              <Sources sources={manifestValidationNote.sources} />
            </Callout>
            <Callout kind="note" title="Two files are not deployed">
              <p>{disabledPoliciesNote.text}</p>
              <Sources sources={disabledPoliciesNote.sources} />
            </Callout>
          </div>

          {cedarPolicies.map((policy) => {
            const purpose = policyPurposeFor(policy.name);
            const headingId = `${policy.name}-heading`;
            return (
              <section key={policy.file} id={policy.name} className={sectionStyles.section} aria-labelledby={headingId}>
                <h2 id={headingId} className={styles.fileHeading}>
                  <code>{policy.file}</code>
                </h2>
                {purpose && (
                  <>
                    <p className={sectionStyles.prose}>{purpose.purpose}</p>
                    <p className={sectionStyles.muted}>
                      {purpose.active
                        ? 'Deployed: listed under policies in manifest.json.'
                        : 'Not deployed: listed under disabledPolicies in manifest.json.'}
                    </p>
                    <Sources sources={purpose.sources} />
                  </>
                )}
                <CodeBlock code={policy.text} language="cedar" />
                <p>
                  <ExternalLink href={policy.githubUrl}>View {policy.file} on GitHub</ExternalLink>
                </p>
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
