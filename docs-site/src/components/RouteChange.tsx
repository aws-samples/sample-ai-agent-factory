import { useContext, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigationType } from 'react-router-dom';
import { MetaContext } from './metaCollector';

/**
 * Route-change behaviour for a single-page app:
 * - scroll to the top instantly (not on back/forward, and not when a hash is present);
 * - move focus to <main id="main-content">;
 * - announce "Navigated to <page title>" in a polite live region.
 * The initial render is left alone so hydration does not steal focus.
 */
export function RouteChange() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const collector = useContext(MetaContext);
  const [announcement, setAnnouncement] = useState('');
  const previousPath = useRef<string | null>(null);

  useEffect(() => {
    if (previousPath.current === null) {
      previousPath.current = location.pathname;
      return;
    }
    if (previousPath.current === location.pathname) return;
    previousPath.current = location.pathname;

    if (location.hash) {
      const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      target?.scrollIntoView();
    } else {
      if (navigationType !== 'POP') {
        window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
      }
      document.getElementById('main-content')?.focus({ preventScroll: true });
    }
    setAnnouncement(`Navigated to ${collector.pageTitle}`);
  }, [location.pathname, location.hash, navigationType, collector]);

  return (
    <div id="route-announcer" role="status" aria-live="polite" aria-atomic="true" className="visually-hidden">
      {announcement}
    </div>
  );
}
