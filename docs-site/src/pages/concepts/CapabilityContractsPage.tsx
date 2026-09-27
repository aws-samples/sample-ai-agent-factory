import { Link } from 'react-router-dom';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ConceptsNav } from './ConceptsNav';
import { SourceLink } from '../../components/SourceLink';
import { capabilities } from '../../content/data';
import { CAPABILITY_CONTRACT_SOURCE, capabilityMatrix } from '../../content/matrix';
import { PATHS } from '../../paths';
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

        <section className={styles.section} aria-labelledby="contracts">
          <h2 id="contracts">Contracts versus implementations</h2>
          <div className={styles.contractBox}>
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
            <p className={styles.contractSource}>
              Source: <SourceLink source={CAPABILITY_CONTRACT_SOURCE} />
            </p>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="capabilities">
          <h2 id="capabilities">The capabilities</h2>
          <p className={styles.prose}>
            The root README lists {capabilities.length} capabilities. Each card names the capability, what it does,
            the reference implementations in this repository, and what a replacement must keep.
          </p>
          <div className={styles.capabilitiesGrid}>
            {capabilities.map((cap) => (
              <article key={cap.id} className={styles.capabilityCard} id={`capability-${cap.id}`}>
                <h3 className={styles.capabilityName}>{cap.name}</h3>
                <p className={styles.capabilityDescription}>{cap.description}</p>
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
                <a href={`#matrix-${cap.id}`} className={styles.matrixLink}>
                  See {cap.name} by project
                </a>
              </article>
            ))}
          </div>
        </section>

        <section className={styles.section} aria-labelledby="matrix">
          <h2 id="matrix">Capability by project</h2>
          <p className={styles.prose}>
            The table states how strongly each project delivers each capability in its tested form. Postures are
            coarse on purpose; the text in each cell carries the nuance and links to the file it comes from.
          </p>
          <PostureLegend />
          <PostureMatrix caption="Capability posture by project, with sources" rowHeader="Capability" rows={matrixRows} />
        </section>

        <section className={styles.section} aria-labelledby="replacing">
          <h2 id="replacing">Replacing an implementation</h2>
          <div className={styles.checklistBox}>
            <p>When replacing a reference implementation with an alternative:</p>
            <ul>
              {REPLACEMENT_CHECKLIST.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <p>
              The tested regions, versions and known limitations per project are collected on the{' '}
              <Link to={PATHS.referenceSupportEnvelope}>support envelope</Link> page.
            </p>
          </div>
        </section>
      </div>
    </div>
  );
}
