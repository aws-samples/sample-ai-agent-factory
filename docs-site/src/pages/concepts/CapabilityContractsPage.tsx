import { ArrowRight } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Card } from '../../components/Card';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { Section } from '../../components/Section';
import { Sources } from '../../components/Sources';
import { capabilities } from '../../content/data';
import { CAPABILITY_CONTRACT_SOURCE, capabilityMatrix } from '../../content/matrix';
import { PATHS } from '../../paths';
import { capabilityIcon } from './capabilityIcons';
import { ConceptsNav } from './ConceptsNav';
import { PostureLegend, PostureMatrix } from './PostureMatrix';
import styles from './CapabilityContractsPage.module.css';

const REPLACEMENT_CHECKLIST = [
  'The replacement must preserve the stated security, identity, tenancy, lifecycle, and evidence contracts.',
  'All positive and adversarial tests must pass with the replacement.',
  'The live support envelope applies only to the exact reference implementation that was tested.',
  'Documentation, runbooks, and threat models may need updates.',
];

export function CapabilityContractsPage() {
  const matrixRows = capabilityMatrix.map((row) => ({ id: row.capabilityId, name: row.name, cells: row.cells }));

  return (
    <div className={styles.page}>
      <PageMeta
        title="Capability contracts"
        description="The Agent Factory capabilities, the contract each one must preserve, the reference implementations in this repository, and how strongly each of the four projects delivers each capability."
      />
      <PageHeader
        eyebrow="Concepts"
        title="Capability contracts"
        lead="Each capability has a contract that any implementation must preserve. The products named below, Amazon Bedrock AgentCore services among them, are the reference choices in this repository, not requirements."
      />

      <div className="container">
        <ConceptsNav />

        <Section id="contracts" title="Contracts versus implementations" flush>
          <Card variant="tinted" tint="blue" padding="lg" className={styles.tintedBox}>
            <p>
              Labels like LLM Gateway, Tool Gateway, agent runtime, memory, identity, registry, policy engine, delivery
              pipeline, and observability describe <strong>architectural capabilities</strong>. A capability contract
              defines the outcomes and controls that any chosen implementation must preserve, regardless of product.
              The repository supplies one integrated implementation so that the contracts can be deployed and tested
              end to end.
            </p>
            <p>
              A substitute is <strong>not</strong> automatically a drop-in configuration change. It can require new
              adapters, IaC, runbooks, threat-model updates, and migration logic. The substitute becomes supported only
              after the same positive and adversarial, mutation, load, rollback, observability, and teardown obligations
              pass for that implementation.
            </p>
            <Sources sources={[CAPABILITY_CONTRACT_SOURCE]} />
          </Card>
        </Section>

        <Section
          id="capabilities"
          title="The capabilities"
          lead={`The root README lists ${capabilities.length} capabilities. Each card names the capability, what it does, the reference implementations in this repository, and what a replacement must keep.`}
        >
          <div className={styles.capabilitiesGrid}>
            {capabilities.map((cap) => {
              const Icon = capabilityIcon(cap.id);
              return (
                <Card as="article" key={cap.id} id={`capability-${cap.id}`} interactive reveal className={styles.capabilityCard}>
                  <div className={styles.capabilityHead}>
                    <span className={styles.capabilityIcon} aria-hidden="true">
                      <Icon size={20} />
                    </span>
                    <h3 className={styles.capabilityName}>{cap.name}</h3>
                  </div>
                  <p>{cap.description}</p>
                  <dl className={styles.capabilityMeta}>
                    <dt>Reference implementations</dt>
                    <dd>
                      <ul className={styles.implTags}>
                        {cap.implementations.map((impl) => (
                          <li key={impl} className={styles.implTag}>
                            {impl}
                          </li>
                        ))}
                      </ul>
                    </dd>
                    {cap.contractNote && (
                      <>
                        <dt>Contract</dt>
                        <dd>{cap.contractNote}</dd>
                      </>
                    )}
                  </dl>
                  <a href={`#matrix-${cap.id}`} className={styles.matrixLink} data-stretch>
                    See {cap.name} by project
                    <ArrowRight size={16} aria-hidden="true" />
                  </a>
                </Card>
              );
            })}
          </div>
        </Section>

        <Section
          id="matrix"
          title="Capability by project"
          lead="The table states how strongly each project delivers each capability in its tested form. Postures are coarse on purpose; the text in each cell carries the nuance and links to the file it comes from."
        >
          <PostureLegend />
          <PostureMatrix caption="Capability posture by project, with sources" rowHeader="Capability" rows={matrixRows} />
        </Section>

        <Section id="replacing" title="Replacing an implementation">
          <Card variant="tinted" tint="amber" padding="lg" className={styles.tintedBox}>
            <p>When replacing a reference implementation with an alternative:</p>
            <ul className={styles.checklist}>
              {REPLACEMENT_CHECKLIST.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <p>
              The tested regions, versions and known limitations per project are collected on the{' '}
              <Link to={PATHS.referenceSupportEnvelope}>support envelope</Link> page.
            </p>
          </Card>
        </Section>
      </div>
    </div>
  );
}
