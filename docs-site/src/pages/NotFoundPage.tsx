import type { MouseEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Home } from 'lucide-react';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { PageMeta } from '../components/PageMeta';
import styles from './NotFoundPage.module.css';

/**
 * 404 page: a slate header band with the two recovery actions. The home action writes
 * `import.meta.env.BASE_URL` (always with the trailing slash) like `HomeLink` does, because
 * react-router renders `to="/"` as the bare basename, which GitHub Pages answers with a
 * redirect; a plain left click still navigates client-side.
 */
export function NotFoundPage() {
  const navigate = useNavigate();
  const goHome = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.altKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    navigate('/');
  };
  return (
    <div className={styles.page}>
      <PageMeta
        title="Page not found"
        description="The page you requested does not exist on the AI Agent Factory site."
        status={404}
      />
      <PageHeader
        eyebrow="404"
        hue="slate"
        title="Page not found"
        lead="The page you are looking for does not exist or has moved."
        actions={
          <>
            <Button href={import.meta.env.BASE_URL} onClick={goHome} iconStart={<Home size={18} />}>
              Go to the home page
            </Button>
            <Button variant="secondary" onClick={() => window.history.back()} iconStart={<ArrowLeft size={18} />}>
              Go back
            </Button>
          </>
        }
      />
    </div>
  );
}
