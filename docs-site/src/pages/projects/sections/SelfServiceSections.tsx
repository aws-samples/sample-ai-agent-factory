import { Link } from 'react-router-dom';
import architectureImg from '../../../../../Agentic-ai-self-service/docs/architecture.jpg';
import canvasImg from '../../../../../Agentic-ai-self-service/docs/images/canvas.png';
import templatesImg from '../../../../../Agentic-ai-self-service/docs/images/templates.png';
import { Callout } from '../../../components/Callout';
import { DocImage } from '../../../components/DocImage';
import { ExternalLink } from '../../../components/ExternalLink';
import { Figure } from '../../../components/Figure';
import { ResponsiveTable } from '../../../components/ResponsiveTable';
import { SourceLink } from '../../../components/SourceLink';
import { docById } from '../../../content/docs';
import { blob } from '../../../content/links';
import {
  BRING_YOUR_OWN_LITELLM,
  BRING_YOUR_OWN_LITELLM_ANCHOR,
  ENTERPRISE_CAPABILITIES_DOC_ID,
  enterpriseCapabilitiesLink,
  firstSignInNote,
  liteLlmNotes,
  liteLlmShapes,
  MCP_CATALOG_PATH,
  mcpCatalogLink,
  selfServiceFigures,
} from '../../../content/projects/self-service-sections';
import { docTitle } from '../../../docs/docModules';
import { Section, Sources, TableCaption } from './shared';
import styles from './ProjectSections.module.css';

/** Hero figure: the canvas screenshot from the README. */
export function SelfServiceHero() {
  return (
    <div className={styles.heroFigure}>
      <DocImage src={canvasImg} alt={selfServiceFigures.canvas.alt} width={1600} height={1000} loading="eager" fetchPriority="high" />
    </div>
  );
}

/** First sign-in note, templates, LiteLLM shapes and the enterprise capabilities pointer. */
export function SelfServiceSections() {
  const readme = docById('self-service/readme');
  const enterprise = docById(ENTERPRISE_CAPABILITIES_DOC_ID);
  return (
    <>
      <Section id="after-the-first-deploy" title="After the first deploy">
        <Callout kind="note" title={firstSignInNote.title}>
          <p>{firstSignInNote.text}</p>
          <Sources sources={firstSignInNote.sources} />
        </Callout>
      </Section>

      <Section id="templates" title="Templates">
        <p className={styles.prose}>
          {selfServiceFigures.templates.text} <SourceLink source={selfServiceFigures.templates.source} />
        </p>
        <Figure
          src={templatesImg}
          alt={selfServiceFigures.templates.alt}
          width={1600}
          height={1000}
          caption="Template gallery screenshot from the README."
        />
      </Section>

      <Section id="bring-your-own-litellm" title="Bring your own LiteLLM">
        <p className={styles.prose}>
          If you already run a LiteLLM proxy, the README describes two roles for it: an MCP gateway for individual
          agents, and the agent catalog behind the Registry. For the gateway role an agent on the canvas has three
          supported shapes.
          {readme && (
            <>
              {' '}
              <Link to={`${readme.route}#${BRING_YOUR_OWN_LITELLM_ANCHOR}`}>Read the full section in the rendered README</Link>.
            </>
          )}
        </p>
        <ResponsiveTable className={styles.table}>
          <TableCaption>The three supported gateway shapes on the canvas, from the README</TableCaption>
          <thead>
            <tr>
              <th scope="col">On the canvas</th>
              <th scope="col">What gets created</th>
              <th scope="col">Use when</th>
            </tr>
          </thead>
          <tbody>
            {liteLlmShapes.map((shape) => (
              <tr key={shape.id}>
                <th scope="row">{shape.onCanvas}</th>
                <td>{shape.created}</td>
                <td>{shape.useWhen}</td>
              </tr>
            ))}
          </tbody>
        </ResponsiveTable>
        <Sources sources={[BRING_YOUR_OWN_LITELLM]} />
        <ul className={styles.list}>
          {liteLlmNotes.map((note) => (
            <li key={note.id}>
              {note.text} <SourceLink source={note.source} />
            </li>
          ))}
        </ul>
      </Section>

      <Section id="enterprise-capabilities" title="Enterprise capabilities">
        <p className={styles.prose}>
          {enterprise ? <Link to={enterprise.route}>{docTitle(enterprise)}</Link> : 'The enterprise capabilities doc'}{' '}
          on this site covers {enterpriseCapabilitiesLink.description}{' '}
          <SourceLink source={enterpriseCapabilitiesLink.source} />
        </p>
      </Section>
    </>
  );
}

/** Figures section: the architecture diagram with its editable source. */
export function SelfServiceFigures() {
  const figure = selfServiceFigures.architecture;
  return (
    <Figure
      src={architectureImg}
      alt={figure.alt}
      width={2400}
      height={1700}
      caption={
        <>
          {figure.caption} <SourceLink source={figure.source} />
        </>
      }
      download={{ href: blob(figure.drawio), label: 'Download the editable .drawio source' }}
    />
  );
}

/** Extra item for the documentation list: the one doc that is not rendered on the site. */
export function SelfServiceExtraDocs() {
  return (
    <li>
      <ExternalLink href={blob(MCP_CATALOG_PATH)}>{mcpCatalogLink.label} (on GitHub)</ExternalLink>: {mcpCatalogLink.description}.{' '}
      <SourceLink source={mcpCatalogLink.source} />
    </li>
  );
}
