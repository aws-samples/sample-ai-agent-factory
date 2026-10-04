import type { HTMLAttributes, ReactNode } from 'react';
import type { JourneyStage } from '../content/data';
import styles from './Card.module.css';

export type CardTint = 'amber' | 'green' | 'violet' | 'blue';

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, 'className' | 'children'> {
  /** Element to render; list items and articles keep their semantics. */
  as?: 'div' | 'li' | 'article' | 'section';
  /** plain: bordered surface; accent: 4 px stage-coloured top border; tinted: soft tint with a left rule. */
  variant?: 'plain' | 'accent' | 'tinted';
  /** Stage for the accent variant (also exposed as data-stage for badges inside). */
  stage?: JourneyStage;
  /** Tint for the tinted variant. */
  tint?: CardTint;
  /** Hover lift and link-coloured border; use when the whole card or its main link is clickable. */
  interactive?: boolean;
  /** Rise into place on scroll (progressive enhancement, see tokens.css). */
  reveal?: boolean;
  padding?: 'sm' | 'md' | 'lg';
  className?: string;
  children: ReactNode;
}

/**
 * The one card surface for the site: one radius, one border, a resting shadow.
 * Pages add their own inner layout through `className`; the card owns only the box.
 * A single inner link marked `data-stretch` makes the whole card clickable while
 * keeping one accessible link.
 */
export function Card({
  as: Tag = 'div',
  variant = 'plain',
  stage,
  tint,
  interactive = false,
  reveal = false,
  padding = 'md',
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <Tag
      {...rest}
      className={[styles.card, className].filter(Boolean).join(' ')}
      data-card
      data-variant={variant}
      data-padding={padding}
      data-stage={stage}
      data-tint={variant === 'tinted' ? (tint ?? 'blue') : undefined}
      data-lift={interactive ? '' : undefined}
      data-reveal={reveal ? '' : undefined}
    >
      {children}
    </Tag>
  );
}
