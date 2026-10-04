declare module 'virtual:repo-index' {
  export interface NotebookEntry {
    /** Repo-relative path, e.g. workshop-building-agentic-ai-platform/source/module-2-llm-gateway/notebooks/step-1-architecture.ipynb */
    path: string;
    /** Folder under source/, e.g. module-2-llm-gateway */
    module: string;
    /** First Markdown heading in the notebook, or a humanised file name. */
    title: string;
    githubUrl: string;
  }

  export interface CedarPolicyEntry {
    /** File name with extension, e.g. forbid-destructive-db.cedar */
    file: string;
    /** File name without extension */
    name: string;
    /** Repo-relative path */
    path: string;
    /** Raw policy text */
    text: string;
    githubUrl: string;
  }

  export const notebooks: NotebookEntry[];
  export const cedarPolicies: CedarPolicyEntry[];
  /** First h1 text of every Markdown file listed in src/content/docs.ts, keyed by repo-relative source path. */
  export const docTitles: Record<string, string>;
}
