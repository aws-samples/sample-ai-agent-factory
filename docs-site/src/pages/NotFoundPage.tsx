import { Link } from 'react-router-dom';
import { Home, ArrowLeft } from 'lucide-react';
import styles from './NotFoundPage.module.css';

export function NotFoundPage() {
  return (
    <div className={styles.page}>
      <div className={styles.content}>
        <h1 className={styles.title}>Page Not Found</h1>
        <p className={styles.message}>
          The page you're looking for doesn't exist or has been moved.
        </p>
        <div className={styles.actions}>
          <Link to="/" className={styles.homeLink}>
            <Home size={18} aria-hidden="true" />
            Go to Home
          </Link>
          <button
            type="button"
            onClick={() => window.history.back()}
            className={styles.backButton}
          >
            <ArrowLeft size={18} aria-hidden="true" />
            Go Back
          </button>
        </div>
      </div>
    </div>
  );
}
