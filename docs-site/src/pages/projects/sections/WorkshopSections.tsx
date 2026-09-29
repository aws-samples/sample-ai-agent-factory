import { Link } from 'react-router-dom';
import { Check } from 'lucide-react';
import { notebooks, type NotebookEntry } from 'virtual:repo-index';
import architectureImg from '../../../../../workshop-building-agentic-ai-platform/static/img/module-1/agentic-ai-platform-architecture.png';
import fastImg from '../../../../../workshop-building-agentic-ai-platform/static/img/module-4/fast-architecture.png';
import { DocImage } from '../../../components/DocImage';
import { ExternalLink } from '../../../components/ExternalLink';
import { Figure } from '../../../components/Figure';
import { NumberedSteps } from '../../../components/NumberedSteps';
import { ResponsiveTable } from '../../../components/ResponsiveTable';
import { SourceLink } from '../../../components/SourceLink';
import {
  CHOOSE_YOUR_TRACK,
  module3bNote,
  notebookFolderNote,
  notebooksRunInIde,
  WHAT_YOULL_BUILD,
  workshopFigures,
  workshopModules,
} from '../../../content/projects/workshop-modules';
import { workshopTracks } from '../../../content/tracks';
import { PATHS } from '../../../paths';
import { Section, Sources, TableCaption } from './shared';
import styles from './ProjectSections.module.css';

function groupByModule(list: NotebookEntry[]): Array<[string, NotebookEntry[]]> {
  const groups = new Map<string, NotebookEntry[]>();
  for (const notebook of list) {
    const bucket = groups.get(notebook.module) ?? [];
    bucket.push(notebook);
    groups.set(notebook.module, bucket);
  }
  return [...groups.entries()];
}

/** Hero figure: the platform architecture image from the README. */
export function WorkshopHero() {
  return (
    <div className={styles.heroFigure}>
      <DocImage src={architectureImg} alt={workshopFigures.architecture.alt} width={2062} height={1094} loading="eager" fetchPriority="high" />
    </div>
  );
}

/** Tracks, modules and notebooks. The three track names are the workshop's own. */
export function WorkshopSections() {
  return (
    <>
      <Section id="tracks" title="Tracks">
        <p className={styles.prose}>
          The workshop has three tracks of its own. All tracks share Module 1, which ends with a track selector.
        </p>
        <ResponsiveTable className={`${styles.table} ${styles.trackMatrix}`}>
          <TableCaption>The three workshop tracks and the modules each one includes, from the README track table</TableCaption>
          <thead>
            <tr>
              <th scope="col">Module</th>
              {workshopTracks.map((track) => (
                <th scope="col" key={track.number}>
                  <span className={styles.trackHead}>
                    <span>
                      {track.number}. {track.name}
                    </span>
                    <span className={styles.trackDuration}>{track.duration}</span>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {workshopModules.map((module) => (
              <tr key={module.id}>
                <th scope="row">
                  {module.name}: {module.title}
                </th>
                {workshopTracks.map((track) => {
                  const included = track.modules.includes(module.name);
                  return (
                    <td key={track.number} className={styles.trackCell} data-included={included ? '' : undefined}>
                      {included ? (
                        <>
                          <Check size={18} aria-hidden="true" />
                          <span className="visually-hidden">included</span>
                        </>
                      ) : (
                        <span className="visually-hidden">not included</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
            <tr>
              <th scope="row">Best for</th>
              {workshopTracks.map((track) => (
                <td key={track.number}>{track.bestFor}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">You do</th>
              {workshopTracks.map((track) => (
                <td key={track.number}>{track.youDo}</td>
              ))}
            </tr>
          </tbody>
        </ResponsiveTable>
        <Sources sources={[CHOOSE_YOUR_TRACK]} />
      </Section>

      <Section id="modules" title="Modules">
        <NumberedSteps stage="learn" dense>
          {workshopModules.map((module) => (
            <NumberedSteps.Item key={module.id} title={`${module.name}: ${module.title}`}>
              <p>{module.summary}</p>
              {module.id === 'module-3b' && (
                <p className={styles.moduleNote}>
                  {module3bNote.text} See <Link to={`${PATHS.conceptsGlossary}#mcp-gateway`}>MCP Gateway in the glossary</Link>.{' '}
                  <Sources inline sources={module3bNote.sources} />
                </p>
              )}
            </NumberedSteps.Item>
          ))}
        </NumberedSteps>
        <Sources sources={[WHAT_YOULL_BUILD]} />
      </Section>

      <Section id="notebooks" title="Notebooks">
        <p className={styles.prose}>
          {notebooksRunInIde.text} <SourceLink source={notebooksRunInIde.source} />
        </p>
        <p className={styles.prose}>
          {notebookFolderNote.text} <Sources inline sources={notebookFolderNote.sources} />
        </p>
        {groupByModule(notebooks).map(([module, list]) => (
          <div key={module} className={styles.notebookGroup}>
            <h3>
              <code>{module}</code>
            </h3>
            <ul className={styles.list}>
              {list.map((notebook) => (
                <li key={notebook.path}>
                  <ExternalLink href={notebook.githubUrl}>{notebook.title}</ExternalLink>
                  <span className={styles.notebookPath}>{notebook.path.split('/').pop()}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </Section>
    </>
  );
}

/** Figures section: FAST architecture, plus provenance for the hero image. */
export function WorkshopFigures() {
  return (
    <>
      <p className={styles.prose}>
        The image in the page header is the platform architecture from the README.{' '}
        <SourceLink source={workshopFigures.architecture.source} />
      </p>
      <Figure
        src={fastImg}
        alt={workshopFigures.fast.alt}
        width={2076}
        height={1738}
        caption={
          <>
            {workshopFigures.fast.caption} <SourceLink source={workshopFigures.fast.source} />
          </>
        }
      />
    </>
  );
}
