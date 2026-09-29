import { Activity, BookMarked, Brain, Coins, Cpu, KeyRound, Plug, Rocket, Route, Scale, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Card } from '../../components/Card';
import { StageBadge } from '../../components/StageBadge';
import { CAPABILITY_LAYERS, postureDots, postureSentence, type DotFill } from '../../content/capabilityLayers';
import { capabilities, projects } from '../../content/data';
import { PATHS } from '../../paths';
import styles from './CapabilityStack.module.css';

/** One lucide icon per capability, always rendered aria-hidden beside the name. */
const CAPABILITY_ICONS: Record<string, LucideIcon> = {
  'llm-gateway': Route,
  'tool-gateway': Plug,
  runtime: Cpu,
  memory: Brain,
  identity: KeyRound,
  registry: BookMarked,
  policy: Scale,
  delivery: Rocket,
  observability: Activity,
  cost: Coins,
};

const FILL_LEGEND: ReadonlyArray<{ fill: DotFill; label: string }> = [
  { fill: 'solid', label: 'Enforced' },
  { fill: 'outline', label: 'Advisory or illustrative' },
  { fill: 'none', label: 'Not part of the project' },
];

/**
 * The shared capabilities as a stack of five layers. Each capability is a link to its
 * row in the capability matrix, with four posture dots (one per project, in stage order)
 * that are decorative; the visually hidden sentence beside them carries the postures.
 */
export function CapabilityStack() {
  return (
    <div className={styles.stack}>
      <div className={styles.legend}>
        <ul className={styles.legendList} aria-label="Dot legend">
          {FILL_LEGEND.map(({ fill, label }) => (
            <li key={fill} className={styles.legendItem}>
              <span className={styles.dot} data-fill={fill} aria-hidden="true" />
              {label}
            </li>
          ))}
        </ul>
        <ul className={styles.legendList} aria-label="Dot order">
          {projects.map((project) => (
            <li key={project.id} className={styles.legendItem}>
              <StageBadge stage={project.stage} variant="outline" label={`${project.stageNumber}. ${project.stageLabel}`} />
              <span className={styles.legendProject}>{project.shortName}</span>
            </li>
          ))}
        </ul>
      </div>

      <ol className={styles.layers} data-capability-stack>
        {CAPABILITY_LAYERS.map((layer) => (
          <Card as="li" key={layer.id} padding="sm" className={styles.layer}>
            <h3 className={styles.layerName}>{layer.name}</h3>
            <ul className={styles.capabilities}>
              {layer.capabilityIds.map((id) => {
                const capability = capabilities.find((entry) => entry.id === id);
                if (!capability) return null;
                const Icon = CAPABILITY_ICONS[id];
                return (
                  <li key={id}>
                    <Link to={`${PATHS.conceptsCapabilityContracts}#matrix-${id}`} className={styles.chip}>
                      {Icon && (
                        <span className={styles.chipIcon} aria-hidden="true">
                          <Icon size={16} />
                        </span>
                      )}
                      <span className={styles.chipName}>{capability.name}</span>
                      <span className={styles.dots}>
                        {postureDots(id).map((dot) => (
                          <span key={dot.projectId} className={styles.dot} data-fill={dot.fill} data-stage={dot.stage} aria-hidden="true" />
                        ))}
                      </span>{' '}
                      <span className="visually-hidden" data-posture-text>
                        {postureSentence(id)}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Card>
        ))}
      </ol>
    </div>
  );
}
