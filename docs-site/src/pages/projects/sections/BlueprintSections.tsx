import servicesSvg from '../../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-aws-services.svg';
import conceptSvg from '../../../../../enterprise-agentic-ai-platform-blueprint/assets/enterprise-agent-factory-concept.svg';
import { DocImage } from '../../../components/DocImage';
import { ExternalLink } from '../../../components/ExternalLink';
import { Figure } from '../../../components/Figure';
import { ResponsiveTable } from '../../../components/ResponsiveTable';
import { SourceLink } from '../../../components/SourceLink';
import { blueprintScps } from '../../../content/facts';
import { blob, tree } from '../../../content/links';
import {
  BLUEPRINT_PACKAGES_DIR,
  BLUEPRINT_TEMPLATES_DIR,
  blueprintFigures,
  GOLDEN_PATHS_SOURCE,
  goldenPaths,
  goldenPathsIntro,
  packageGroups,
} from '../../../content/projects/blueprint-golden-paths';
import { Section, Sources, TableCaption } from './shared';
import styles from './ProjectSections.module.css';

/** Hero figure: Figure 1 of the README with its editable source. */
export function BlueprintHero() {
  const figure = blueprintFigures.concept;
  return (
    <div>
      <div className={styles.heroFigure}>
        <DocImage src={conceptSvg} alt={figure.alt} width={1840} height={1330} loading="eager" fetchPriority="high" />
      </div>
      <p className={styles.heroDownload}>
        <ExternalLink href={blob(figure.drawio)}>Download the editable .drawio source of Figure 1</ExternalLink>
      </p>
    </div>
  );
}

/** SCP list, golden paths and the repository map. */
export function BlueprintSections() {
  const packageCount = packageGroups.reduce((sum, group) => sum + group.packages.length, 0);
  return (
    <>
      <Section id="service-control-policies" title="Service control policies">
        <p className={styles.prose}>
          The Blueprint ships {blueprintScps.length} service control policies as TypeScript definitions. Each entry
          links to its source file.
        </p>
        <ol className={styles.list}>
          {blueprintScps.map((scp) => (
            <li key={scp.id}>
              <code>{scp.id}</code> {scp.title}:{' '}
              <SourceLink source={{ file: scp.file }}>{scp.file.split('/').pop()}</SourceLink>
            </li>
          ))}
        </ol>
      </Section>

      <Section id="golden-paths" title="Golden paths">
        <p className={styles.prose}>
          {goldenPathsIntro.text} <SourceLink source={goldenPathsIntro.source} />
        </p>
        <ResponsiveTable className={styles.table}>
          <TableCaption>The {goldenPaths.length} templates under blueprints/, from README section 7</TableCaption>
          <thead>
            <tr>
              <th scope="col">Template</th>
              <th scope="col">Framework</th>
              <th scope="col">Best fit</th>
              <th scope="col">Enterprise contract</th>
            </tr>
          </thead>
          <tbody>
            {goldenPaths.map((path) => (
              <tr key={path.name}>
                <th scope="row">
                  <ExternalLink href={tree(`${BLUEPRINT_TEMPLATES_DIR}/${path.name}`)}>
                    <code>{path.name}</code>
                  </ExternalLink>
                </th>
                <td>{path.framework}</td>
                <td>{path.bestFit}</td>
                <td>{path.contract}</td>
              </tr>
            ))}
          </tbody>
        </ResponsiveTable>
        <Sources sources={[GOLDEN_PATHS_SOURCE]} />
      </Section>

      <Section id="repository-map" title="Repository map">
        <p className={styles.prose}>
          The packages/ folder holds {packageCount} packages. The groups below are a reading aid; the names are as in
          the repository. <ExternalLink href={tree(BLUEPRINT_PACKAGES_DIR)}>Browse packages/ on GitHub</ExternalLink>
        </p>
        <div className={styles.groups}>
          {packageGroups.map((group) => (
            <div key={group.id} className={styles.group}>
              <h3>{group.name}</h3>
              <ul>
                {group.packages.map((name) => (
                  <li key={name}>
                    <code>{name}</code>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </Section>
    </>
  );
}

/** Figures section: Figure 2 of the README with its editable source. */
export function BlueprintFigures() {
  const figure = blueprintFigures.services;
  return (
    <Figure
      src={servicesSvg}
      alt={figure.alt}
      width={1920}
      height={1470}
      caption={
        <>
          {figure.caption} <SourceLink source={figure.sources[0]} />
        </>
      }
      download={{ href: blob(figure.drawio), label: 'Download the editable .drawio source' }}
    />
  );
}
