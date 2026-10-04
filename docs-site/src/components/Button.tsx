import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router-dom';
import { ExternalLink } from './ExternalLink';
import styles from './Button.module.css';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface BaseProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Decorative icon before the label (rendered aria-hidden). */
  iconStart?: ReactNode;
  /** Decorative icon after the label (rendered aria-hidden); nudges on hover. */
  iconEnd?: ReactNode;
  className?: string;
  children: ReactNode;
}

/** Internal navigation via react-router. */
type AsLink = BaseProps & { to: string; href?: never } & Omit<LinkProps, 'to' | 'className' | 'children'>;
/** Plain anchor: in-page fragment, or an external URL when `external` is set (opens in a new tab). */
type AsAnchor = BaseProps & {
  href: string;
  to?: never;
  external?: boolean;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href' | 'className' | 'children' | 'target' | 'rel'>;
/** Real button. */
type AsButton = BaseProps & { to?: never; href?: never } & Omit<
    ButtonHTMLAttributes<HTMLButtonElement>,
    'className' | 'children'
  >;

export type ButtonProps = AsLink | AsAnchor | AsButton;

/**
 * The one button style for the site. Renders a react-router `Link` when `to` is
 * given, an anchor (wrapped in `ExternalLink` when `external`) when `href` is
 * given, and a `<button>` otherwise. Colours come from tokens, so the same
 * markup works inside `.on-dark` containers without extra props.
 */
export function Button(props: ButtonProps) {
  const { variant = 'primary', size = 'md', iconStart, iconEnd, className, children } = props;
  const classes = [styles.button, className].filter(Boolean).join(' ');
  const data = { 'data-variant': variant, 'data-size': size } as const;
  const content = (
    <>
      {iconStart && (
        <span className={styles.icon} aria-hidden="true">
          {iconStart}
        </span>
      )}
      <span className={styles.label}>{children}</span>
      {iconEnd && (
        <span className={`${styles.icon} ${styles.iconEnd}`} data-icon-end aria-hidden="true">
          {iconEnd}
        </span>
      )}
    </>
  );

  if ('to' in props && props.to !== undefined) {
    const { to, ...rest } = withoutBase(props);
    return (
      <Link {...rest} {...data} to={to} className={classes}>
        {content}
      </Link>
    );
  }

  if ('href' in props && props.href !== undefined) {
    const { href, external, ...rest } = withoutBase(props);
    if (external) {
      return (
        <ExternalLink {...rest} {...data} href={href} className={classes} iconSize={16} hideIcon={Boolean(iconEnd)}>
          {content}
        </ExternalLink>
      );
    }
    return (
      <a {...rest} {...data} href={href} className={classes}>
        {content}
      </a>
    );
  }

  const { type, ...rest } = withoutBase(props);
  return (
    <button {...rest} {...data} type={type ?? 'button'} className={classes}>
      {content}
    </button>
  );
}

const BASE_KEYS = ['variant', 'size', 'iconStart', 'iconEnd', 'className', 'children'] as const;

/** Drop the presentational props so the rest can spread onto the rendered element. */
function withoutBase<T extends BaseProps>(props: T): Omit<T, (typeof BASE_KEYS)[number]> {
  const rest = { ...props } as Record<string, unknown>;
  for (const key of BASE_KEYS) delete rest[key];
  return rest as Omit<T, (typeof BASE_KEYS)[number]>;
}
