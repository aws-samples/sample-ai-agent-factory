import { Link } from 'react-router-dom';
import { cedarPolicies } from 'virtual:repo-index';
import { Callout } from '../../../components/Callout';
import { CodeBlock } from '../../../components/CodeBlock';
import { ResponsiveTable } from '../../../components/ResponsiveTable';
import { SourceLink } from '../../../components/SourceLink';
import { docById } from '../../../content/docs';
import {
  businessHoursNote,
  claims,
  DEMO_QUERIES_SOURCE,
  demoQueries,
  FEATURED_POLICY_FILE,
  featuredPolicyNotes,
  hidingWinsNote,
  honestNotes,
  module3bComparison,
  splitCedarStatements,
  VERIFIED_ARCHITECTURE,
} from '../../../content/projects/mcp-gateway-demo';
import { PATHS } from '../../../paths';
import { RequestFlowStrip } from './RequestFlowStrip';
import { Section, Sources, TableCaption } from './shared';
import styles from './ProjectSections.module.css';

/** Hero figure: the request-flow strip (the README has no image). */
export function McpGatewayHero() {
  return <RequestFlowStrip />;
}

/** Demo table, the featured Cedar policy, the claims table and the honest notes. */
export function McpGatewaySections() {
  const featured = cedarPolicies.find((policy) => policy.file === FEATURED_POLICY_FILE);
  const statements = featured ? splitCedarStatements(featured.text) : [];
  return (
    <>
      <Section id="see-it-work" title="See it work">
        <p className={styles.prose}>
          The README walkthrough drives the governed tools from a real coding agent. The outcome is produced by the
          gateway, not the agent. {hidingWinsNote.text} <SourceLink source={hidingWinsNote.source} />
        </p>
        <ResponsiveTable className={styles.table}>
          <TableCaption>Six queries from the README walkthrough and the governance outcome each one produces</TableCaption>
          <thead>
            <tr>
              <th scope="col">Query</th>
              <th scope="col">Ask the agent to</th>
              <th scope="col">Expected governance outcome</th>
              <th scope="col">Enforced by</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {demoQueries.map((query) => (
              <tr key={query.id}>
                <th scope="row">{query.id}</th>
                <td>{query.ask}</td>
                <td>{query.outcome}</td>
                <td>{query.enforcedBy}</td>
                <td>
                  <SourceLink source={query.source}>README row {query.id}</SourceLink>
                </td>
              </tr>
            ))}
          </tbody>
        </ResponsiveTable>
        <p className={styles.muted}>
          {businessHoursNote.text} <SourceLink source={businessHoursNote.source} />
        </p>
        <Sources sources={[DEMO_QUERIES_SOURCE]} />
      </Section>

      <Section id="compared-with-the-workshop" title="Compared with the workshop">
        <Callout kind="note" title={module3bComparison.title}>
          <p>{module3bComparison.text}</p>
          <p>
            The glossary lists what <Link to={`${PATHS.conceptsGlossary}#mcp-gateway`}>MCP Gateway</Link> means in each
            of the four projects.
          </p>
          <Sources sources={module3bComparison.sources} />
        </Callout>
      </Section>

      <Section id="cedar-policy" title="Cedar policy">
        <p className={styles.prose}>
          <code>{FEATURED_POLICY_FILE}</code> carries no role or identity gate, so every statement in it applies to the
          seeded users. The CDK deploys one policy per statement. The notes below follow the statements in file order.
        </p>
        {featured && <CodeBlock code={featured.text} language="cedar" />}
        <ol className={styles.list}>
          {statements.map((statement, index) => {
            const note = featuredPolicyNotes.find(
              (candidate) => candidate.effect === statement.effect && candidate.action === statement.action,
            );
            return (
              <li key={`${statement.effect}-${statement.action}-${index}`}>
                <strong>
                  {statement.effect} <code>{statement.action}</code>.
                </strong>{' '}
                {note?.note}
                {note && <Sources sources={note.sources} />}
              </li>
            );
          })}
        </ol>
        <p>
          <Link to={PATHS.mcpGatewayPolicies}>All {cedarPolicies.length} policy files, each with a purpose line</Link>
        </p>
      </Section>

      <Section id="claims" title="Claims that reach Cedar">
        <p className={styles.prose}>
          The gateway validates the Cognito access token, and its claims become Cedar principal tags. The ID token is
          rejected, so claims that live only there never reach a policy.
        </p>
        <ResponsiveTable className={styles.table}>
          <TableCaption>Cognito token claims and whether each one becomes a Cedar principal tag</TableCaption>
          <thead>
            <tr>
              <th scope="col">Claim</th>
              <th scope="col">Token</th>
              <th scope="col">Reaches Cedar</th>
              <th scope="col">Note</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {claims.map((claim) => (
              <tr key={claim.claim}>
                <th scope="row">
                  <code>{claim.claim}</code>
                </th>
                <td>{claim.token}</td>
                <td>
                  {claim.reachesCedar ? (
                    <span className={styles.ok}>Yes, as a tag</span>
                  ) : (
                    <span className={styles.no}>No</span>
                  )}
                </td>
                <td>{claim.note}</td>
                <td>
                  <SourceLink source={claim.source} />
                </td>
              </tr>
            ))}
          </tbody>
        </ResponsiveTable>
      </Section>

      <Section id="before-you-demo" title="Before you demo">
        <div className={styles.callouts}>
          {honestNotes.map((note) => (
            <Callout key={note.id} kind="important" title={note.title}>
              <p>{note.text}</p>
              <Sources sources={note.sources} />
            </Callout>
          ))}
        </div>
      </Section>
    </>
  );
}

/** Figures section: the README has no image, so point at the source of the strip. */
export function McpGatewayFigures() {
  const readme = docById('mcp-gateway/readme');
  return (
    <p className={styles.prose}>
      The gateway README has no architecture image. The request path in the page header is drawn from its Verified
      architecture section. <SourceLink source={VERIFIED_ARCHITECTURE} />
      {readme && (
        <>
          {' '}
          Read it in the <Link to={`${readme.route}#verified-architecture`}>rendered README</Link>.
        </>
      )}
    </p>
  );
}
