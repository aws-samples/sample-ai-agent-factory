import { BookOpen, CircleSlash, Eye, Minus, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { ResponsiveTable } from '../../components/ResponsiveTable';
import { SourceLink } from '../../components/SourceLink';
import { projects, type ProjectId } from '../../content/data';
import type { Source } from '../../content/facts';
import { POSTURE_LABELS, type MatrixCell, type Posture } from '../../content/matrix';
import { projectPath } from '../../paths';
import styles from './PostureMatrix.module.css';

/**
 * Link text for a cell source. Workshop pages are all named index.en.md, so
 * the parent folder is included for those; other files keep the file name.
 */
function sourceLabel(source: Source): string {
  if (!source.file) return source.label ?? source.url ?? 'Source';
  const parts = source.file.split('/');
  const file = parts[parts.length - 1] ?? source.file;
  const folder = parts.length > 1 ? parts[parts.length - 2] : undefined;
  const name = file === 'index.en.md' && folder ? `${folder}/${file}` : file;
  return source.heading ? `${name}: ${source.heading}` : name;
}

/** One row of a posture matrix: a label plus one cell per project. */
export interface PostureMatrixRow {
  id: string;
  name: string;
  cells: Record<ProjectId, MatrixCell>;
}

export interface PostureMatrixProps {
  /** Table caption (visible). */
  caption: string;
  /** Header of the first column, for example "Capability" or "Control". */
  rowHeader: string;
  rows: PostureMatrixRow[];
}

const POSTURE_ORDER: Posture[] = ['enforced', 'advisory', 'illustrative', 'outside-envelope', 'not-applicable'];

/** Icon per posture: the shape, not only the colour, tells postures apart. */
const POSTURE_ICONS = {
  enforced: ShieldCheck,
  advisory: Eye,
  illustrative: BookOpen,
  'outside-envelope': CircleSlash,
  'not-applicable': Minus,
} as const;

const POSTURE_CLASS: Record<Posture, string> = {
  enforced: styles.enforced,
  advisory: styles.advisory,
  illustrative: styles.illustrative,
  'outside-envelope': styles.outsideEnvelope,
  'not-applicable': styles.notApplicable,
};

/** Posture label with an aria-hidden icon. The text is always visible. */
export function PostureBadge({ posture }: { posture: Posture }) {
  const Icon = POSTURE_ICONS[posture];
  return (
    <span className={`${styles.badge} ${POSTURE_CLASS[posture]}`}>
      <Icon size={14} aria-hidden="true" className={styles.badgeIcon} />
      {POSTURE_LABELS[posture].label}
    </span>
  );
}

/** Legend explaining the five postures. Render once above a matrix. */
export function PostureLegend() {
  return (
    <ul className={styles.legend} aria-label="Posture legend">
      {POSTURE_ORDER.map((posture) => (
        <li key={posture} className={styles.legendItem}>
          <PostureBadge posture={posture} />
          <span className={styles.legendMeaning}>{POSTURE_LABELS[posture].meaning}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Capability-by-project or control-by-project table. Every cell shows the
 * posture as text with a distinct icon, a short statement, and a link to the
 * repository file the statement comes from.
 */
export function PostureMatrix({ caption, rowHeader, rows }: PostureMatrixProps) {
  return (
    <>
      <p className={styles.scrollHint}>The table is wider than the screen. Scroll it sideways to see every project.</p>
      <ResponsiveTable className={styles.table}>
        <caption className={styles.caption}>{caption}</caption>
      <thead>
        <tr>
          <th scope="col" className={styles.rowHeader}>
            {rowHeader}
          </th>
          {projects.map((project) => (
            <th scope="col" key={project.id} className={styles.colHeader}>
              <Link to={projectPath(project.id)}>{project.shortName}</Link>
              <span className={styles.stageLabel} data-stage={project.stage}>
                {project.stageLabel}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id} id={`matrix-${row.id}`}>
            <th scope="row" className={styles.rowHeader}>
              {row.name}
            </th>
            {projects.map((project) => {
              const cell = row.cells[project.id];
              return (
                <td key={project.id} className={styles.cell}>
                  <PostureBadge posture={cell.posture} />
                  <p className={styles.cellText}>{cell.text}</p>
                  {cell.source && (
                    <p className={styles.cellSource}>
                      <SourceLink source={cell.source}>{sourceLabel(cell.source)}</SourceLink>
                    </p>
                  )}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
      </ResponsiveTable>
    </>
  );
}
