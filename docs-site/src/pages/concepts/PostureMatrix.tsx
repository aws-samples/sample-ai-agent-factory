import { BookOpen, CircleSlash, Eye, Minus, ShieldCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Card } from '../../components/Card';
import { ResponsiveTable } from '../../components/ResponsiveTable';
import { SourceLink } from '../../components/SourceLink';
import { projects, type ProjectId } from '../../content/data';
import type { Source } from '../../content/facts';
import { POSTURE_LABELS, type MatrixCell, type Posture } from '../../content/matrix';
import { projectPath } from '../../paths';
import { STAGE_ICONS } from '../../stage';
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

/** Posture label with an aria-hidden icon. The text is always visible. */
export function PostureBadge({ posture }: { posture: Posture }) {
  const Icon = POSTURE_ICONS[posture];
  return (
    <span className={styles.badge} data-posture={posture}>
      <Icon size={14} aria-hidden="true" className={styles.badgeIcon} />
      {POSTURE_LABELS[posture].label}
    </span>
  );
}

/** Legend explaining the five postures. Render once above a matrix. */
export function PostureLegend() {
  return (
    <Card padding="sm" className={styles.legend}>
      <ul className={styles.legendList} aria-label="Posture legend">
        {POSTURE_ORDER.map((posture) => (
          <li key={posture} className={styles.legendItem}>
            <PostureBadge posture={posture} />
            <span className={styles.legendMeaning}>{POSTURE_LABELS[posture].meaning}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/**
 * Capability-by-project or control-by-project heat-map. Every cell is tinted by
 * posture and also shows the posture as text with a distinct icon, a short
 * statement, and a link to the repository file the statement comes from, so
 * colour is never the only cue. Row-header cells carry `id="matrix-<rowId>"`
 * so other pages can link to a row.
 */
export function PostureMatrix({ caption, rowHeader, rows }: PostureMatrixProps) {
  return (
    <>
      <p className={styles.scrollHint}>The table is wider than the screen. Scroll it sideways to see every project.</p>
      <ResponsiveTable className={styles.table} data-posture-matrix>
        <caption className={styles.caption}>{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className={styles.rowHeader}>
              {rowHeader}
            </th>
            {projects.map((project) => {
              const Icon = STAGE_ICONS[project.stage];
              return (
                <th scope="col" key={project.id} className={styles.colHeader} data-stage={project.stage}>
                  <span className={styles.colHeaderName}>
                    <Icon size={18} aria-hidden="true" className={styles.stageIcon} />
                    <Link to={projectPath(project.id)}>{project.shortName}</Link>
                  </span>
                  <span className={styles.stageLabel}>{project.stageLabel}</span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className={styles.row}>
              <th scope="row" id={`matrix-${row.id}`} className={styles.rowHeader}>
                {row.name}
              </th>
              {projects.map((project) => {
                const cell = row.cells[project.id];
                return (
                  <td key={project.id} className={styles.cell} data-posture={cell.posture}>
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
