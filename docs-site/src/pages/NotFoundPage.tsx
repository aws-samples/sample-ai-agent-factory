import { Home, ArrowLeft } from 'lucide-react';
import { HomeLink } from '../components/HomeLink';
import { PageMeta } from '../components/PageMeta';
import styles from './NotFoundPage.module.css';

export function NotFoundPage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Page not found"
        description="The page you requested does not exist on the AI Agent Factory site."
        status={404}
      />
      <div className={styles.content}>
        <h1 className={styles.title}>Page not found</h1>
        <p className={styles.message}>The page you are looking for does not exist or has moved.</p>
        <div className={styles.actions}>
          <HomeLink className={styles.homeLink}>
            <Home size={18} aria-hidden="true" />
            Go to Home
          </HomeLink>
          <button type="button" onClick={() => window.history.back()} className={styles.backButton}>
            <ArrowLeft size={18} aria-hidden="true" />
            Go back
          </button>
        </div>
      </div>
    </div>
  );
}
