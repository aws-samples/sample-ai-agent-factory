import { cedarPolicies } from 'virtual:repo-index';
import { CircleCheck, CircleMinus } from 'lucide-react';
import { Button } from '../../components/Button';
import { Callout } from '../../components/Callout';
import { Card } from '../../components/Card';
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
  policyPurposes,
} from '../../content/projects/mcp-gateway-demo';
import { projectPath } from '../../paths';
import { Section, Sources } from './sections/shared';
import sectionStyles from './sections/ProjectSections.module.css';
import styles from './PoliciesPage.module.css';

/** Deployed or not deployed, as the manifest lists the file. Icon and text together, never colour alone. */
function DeployedBadge({ active }: { active: boolean }) {
  const Icon = active ? CircleCheck : CircleMinus;
  return (
    <span className={styles.deployBadge} data-deployed={active ? '' : undefined}>
      <Icon size={14} aria-hidden="true" />
      {active ? 'Deployed' : 'Not deployed'}
    </span>
  );
}

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
            <Button to={projectPath('mcp-gateway')}>Back to the project page</Button>
            <Button href={tree(GATEWAY_POLICIES_DIR)} external variant="secondary">
              policies/ on GitHub
            </Button>
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

          <Section
            id="summary"
            title="The policies at a glance"
            lead="One card per file: its purpose and whether the manifest deploys it. Each card jumps to the full policy below."
          >
            <ul className={styles.summaryGrid}>
              {policyPurposes.map((purpose) => (
                <Card as="li" key={purpose.name} interactive reveal padding="sm" className={styles.summaryCard}>
                  <div className={styles.summaryHead}>
                    <a href={`#${purpose.name}`} className={styles.summaryName} data-stretch>
                      <code>{purpose.name}.cedar</code>
                    </a>
                    <DeployedBadge active={purpose.active} />
                  </div>
                  <p className={styles.summaryPurpose}>{purpose.purpose}</p>
                </Card>
              ))}
            </ul>
          </Section>

          {cedarPolicies.map((policy) => {
            const purpose = policyPurposeFor(policy.name);
            return (
              <Section
                key={policy.file}
                id={policy.name}
                title={<code className={styles.fileCode}>{policy.file}</code>}
                badge={purpose ? <DeployedBadge active={purpose.active} /> : undefined}
              >
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
              </Section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
