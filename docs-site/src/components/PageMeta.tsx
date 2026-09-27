import { useContext, useEffect, useMemo, type ReactNode } from 'react';
import { createMetaCollector, formatTitle, MetaContext, type MetaCollector } from './metaCollector';

export interface PageMetaProps {
  /** Page title without the site suffix, e.g. "Security". */
  title: string;
  /** One or two sentences; used for meta description, Open Graph and llms.txt. */
  description: string;
  /** Use `title` verbatim as the document title (Home only). */
  titleIsFull?: boolean;
  /** HTTP-equivalent status; the prerender fails on anything but 200 for listed routes. */
  status?: number;
}

/**
 * Declares the page title and description. Every page renders exactly one.
 * Server: writes into the MetaCollector from context during render.
 * Client: sets document.title in an effect.
 */
export function PageMeta({ title, description, titleIsFull = false, status = 200 }: PageMetaProps) {
  const collector = useContext(MetaContext);
  const fullTitle = formatTitle(title, titleIsFull);

  collector.title = fullTitle;
  collector.pageTitle = title;
  collector.description = description;
  collector.status = status;

  useEffect(() => {
    document.title = fullTitle;
  }, [fullTitle]);

  return null;
}

export interface MetaProviderProps {
  /** Supply a collector to read results back (server); omitted on the client. */
  collector?: MetaCollector;
  children: ReactNode;
}

export function MetaProvider({ collector, children }: MetaProviderProps) {
  const value = useMemo(() => collector ?? createMetaCollector(), [collector]);
  return <MetaContext.Provider value={value}>{children}</MetaContext.Provider>;
}
