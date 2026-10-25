import { createContext } from 'react';

export const SITE_NAME = 'Agentic AI Factory';
export const DEFAULT_DESCRIPTION =
  'Agentic AI Factory: enterprise agentic AI samples for AWS with Amazon Bedrock and Amazon Bedrock AgentCore';

/**
 * Per-render collector. On the server, PageMeta writes into it during render
 * and scripts/prerender.mjs reads it back. On the client, RouteChange reads
 * the page title from it to announce navigation.
 */
export interface MetaCollector {
  /** Full document title, e.g. "Security | Agentic AI Factory". */
  title: string;
  /** Page part of the title, e.g. "Security". */
  pageTitle: string;
  description: string;
  /** 200 for normal pages, 404 for the not-found page. */
  status: number;
}

export function createMetaCollector(): MetaCollector {
  return { title: SITE_NAME, pageTitle: SITE_NAME, description: DEFAULT_DESCRIPTION, status: 200 };
}

export function formatTitle(pageTitle: string, titleIsFull = false): string {
  return titleIsFull ? pageTitle : `${pageTitle} | ${SITE_NAME}`;
}

export const MetaContext = createContext<MetaCollector>(createMetaCollector());
