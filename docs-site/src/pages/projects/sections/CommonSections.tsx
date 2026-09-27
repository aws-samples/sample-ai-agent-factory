import type { ReactNode } from 'react';
import { Callout } from '../../../components/Callout';
import { CodeBlock } from '../../../components/CodeBlock';
import type { ProjectId } from '../../../content/data';
import { githubUrl } from '../../../content/facts';
import { getEvidence } from '../../../content/projects/evidence';
import { getWhatItIs } from '../../../content/projects/what-it-is';
import { Section, Sources } from './shared';
import styles from './ProjectSections.module.css';

/**
 * "What it is": the project's feature bullets with the repository text behind
 * each one. When every bullet shares one source the link is printed once.
 */
export function WhatItIsSection({ projectId }: { projectId: ProjectId }) {
  const items = getWhatItIs(projectId);
  const distinct = new Set(items.flatMap((item) => item.sources.map(githubUrl)));
  const perItem = distinct.size > 1;
  return (
    <Section id="what-it-is" title="What it is">
      <ul className={styles.list}>
        {items.map((item) => (
          <li key={item.text}>
            {item.text}
            {perItem && (
              <>
                {' '}
                <Sources inline sources={item.sources} />
              </>
            )}
          </li>
        ))}
      </ul>
      {!perItem && items[0] && <Sources sources={items[0].sources} />}
    </Section>
  );
}

/**
 * "Evidence": what the project tests, the command, what a pass proves, and how
 * much of it runs against live AWS. `children` renders between the live-AWS
 * note and the items (the Blueprint's validated envelope goes there).
 */
export function EvidenceSection({ projectId, children }: { projectId: ProjectId; children?: ReactNode }) {
  const data = getEvidence(projectId);
  return (
    <Section id="evidence" title="Evidence">
      <p className={styles.prose}>{data.intro}</p>
      <Callout kind="note" title="What runs against live AWS">
        <p>{data.live.text}</p>
        <Sources sources={data.live.sources} />
      </Callout>
      {children}
      {data.items.map((item) => (
        <div key={item.id} className={styles.evidenceItem}>
          <h3>{item.what}</h3>
          {item.command && <CodeBlock code={item.command} language="bash" />}
          <p className={styles.prose}>
            <strong>What a pass proves:</strong> {item.proves}
          </p>
          {item.list && (
            <ol className={styles.list}>
              {item.list.map((entry) => (
                <li key={entry}>{entry}</li>
              ))}
            </ol>
          )}
          <Sources sources={item.sources} />
        </div>
      ))}
    </Section>
  );
}
