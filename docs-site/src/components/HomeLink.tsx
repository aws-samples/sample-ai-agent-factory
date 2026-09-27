import type { AnchorHTMLAttributes, MouseEvent } from 'react';
import { useNavigate } from 'react-router-dom';

export type HomeLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'>;

/**
 * Link to the site home. React Router renders `to="/"` as the bare basename
 * (`/sample-ai-agent-factory`, no trailing slash), which GitHub Pages answers with a
 * redirect; this link writes `import.meta.env.BASE_URL` (always with the trailing slash)
 * and still navigates client-side on a plain left click.
 */
export function HomeLink({ children, onClick, target, ...rest }: HomeLinkProps) {
  const navigate = useNavigate();
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    if (event.button !== 0 || event.metaKey || event.altKey || event.ctrlKey || event.shiftKey) return;
    if (target && target !== '_self') return;
    event.preventDefault();
    navigate('/');
  };
  return (
    <a href={import.meta.env.BASE_URL} target={target} onClick={handleClick} {...rest}>
      {children}
    </a>
  );
}
