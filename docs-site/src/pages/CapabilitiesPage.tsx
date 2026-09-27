import { capabilities } from '../content/data';
import styles from './CapabilitiesPage.module.css';

export function CapabilitiesPage() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerContent}>
          <h1>Capabilities</h1>
          <p>
            The architectural capabilities that together form an AI Agent Factory.
            Each capability has a contract; implementations are replaceable when contracts are preserved.
          </p>
        </div>
      </header>

      <div className={styles.content}>
        <div className={styles.contentContent}>
          <section className={styles.introSection}>
            <div className={styles.introBox}>
              <h2>Capability Contracts</h2>
              <p>
                Labels like LLM Gateway, Tool Gateway, agent runtime, memory, identity, registry,
                policy engine, delivery pipeline, and observability describe <strong>architectural
                capabilities</strong>. The repository supplies one integrated implementation so that
                the contracts can be deployed and tested end to end.
              </p>
              <p>
                A substitute is <strong>not</strong> automatically a drop-in configuration change.
                It can require new adapters, IaC, runbooks, threat-model updates, and migration logic.
                The substitute becomes supported only after the same positive/adversarial, mutation,
                load, rollback, observability, and teardown obligations pass for that implementation.
              </p>
            </div>
          </section>

          <section className={styles.capabilitiesSection}>
            <div className={styles.capabilitiesGrid}>
              {capabilities.map((cap) => (
                <article key={cap.id} className={styles.capabilityCard}>
                  <h3 className={styles.capabilityName}>{cap.name}</h3>
                  <p className={styles.capabilityDescription}>{cap.description}</p>

                  <div className={styles.capabilityMeta}>
                    <div className={styles.metaItem}>
                      <h4>Reference Implementations</h4>
                      <div className={styles.implTags}>
                        {cap.implementations.map((impl) => (
                          <span key={impl} className={styles.implTag}>{impl}</span>
                        ))}
                      </div>
                    </div>

                    {cap.contractNote && (
                      <div className={styles.contractNote}>
                        <h4>Contract</h4>
                        <p>{cap.contractNote}</p>
                      </div>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className={styles.noticeSection}>
            <div className={styles.noticeBox}>
              <h3>Replacing an Implementation</h3>
              <p>
                When replacing a reference implementation with an alternative:
              </p>
              <ul>
                <li>The replacement must preserve the stated security, identity, tenancy, lifecycle, and evidence contracts</li>
                <li>All positive and adversarial tests must pass with the replacement</li>
                <li>The live support envelope applies only to the exact reference implementation that was tested</li>
                <li>Documentation, runbooks, and threat models may need updates</li>
              </ul>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
