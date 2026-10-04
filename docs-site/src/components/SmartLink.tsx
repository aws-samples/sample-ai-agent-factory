import type { AnchorHTMLAttributes } from 'react';
import { Link } from 'react-router-dom';
import { ExternalLink } from './ExternalLink';

const ABSOLUTE_URL = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;

/**
 * Link component for MDX `a` elements and page copy:
 * - http(s) and other absolute URLs open in a new tab via ExternalLink;
 * - same-page anchors and mailto: stay plain anchors;
 * - everything else is a site route rendered with React Router's <Link>.
 */
export function SmartLink({ href = '', children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (href.startsWith('#') || href.startsWith('mailto:')) {
    return (
      <a href={href} {...rest}>
        {children}
      </a>
    );
  }
  if (ABSOLUTE_URL.test(href)) {
    return (
      <ExternalLink href={href} {...rest}>
        {children}
      </ExternalLink>
    );
  }
  return (
    <Link to={href} {...rest}>
      {children}
    </Link>
  );
}
