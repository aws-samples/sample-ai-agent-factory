import { Card } from '../../../components/Card';
import { SourceLink } from '../../../components/SourceLink';
import { requestFlow, VERIFIED_ARCHITECTURE } from '../../../content/projects/mcp-gateway-demo';
import styles from './ProjectSections.module.css';

/**
 * HTML rendering of the gateway request path (the README has no image for it).
 * Six numbered steps drawn from the "Verified architecture" diagram.
 */
export function RequestFlowStrip() {
  return (
    <figure className={styles.flow} aria-labelledby="request-flow-caption">
      <ol className={styles.flowList}>
        {requestFlow.map((step, index) => (
          <Card as="li" key={step.id} padding="sm" className={styles.flowStep}>
            <span className={styles.flowIndex} aria-hidden="true">
              {index + 1}
            </span>
            <span className={styles.flowName}>{step.name}</span>
            <span className={styles.flowDetail}>{step.detail}</span>
          </Card>
        ))}
      </ol>
      <figcaption id="request-flow-caption" className={styles.flowCaption}>
        Request path as drawn in the README. <SourceLink source={VERIFIED_ARCHITECTURE} />
      </figcaption>
    </figure>
  );
}
