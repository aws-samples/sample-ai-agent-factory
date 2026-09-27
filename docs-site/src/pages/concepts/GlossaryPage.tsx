import { Link } from 'react-router-dom';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { ConceptsNav } from './ConceptsNav';
import { SourceLink } from '../../components/SourceLink';
import { StageBadge } from '../../components/StageBadge';
import { getProjectById } from '../../content/data';
import { glossary, type GlossaryTerm } from '../../content/glossary';
import { projectPath } from '../../paths';
import styles from './GlossaryPage.module.css';

/** First letter (A to Z or 0 to 9) a term sorts under. */
function groupKey(term: GlossaryTerm): string {
  const first = term.term.replace(/^[^A-Za-z0-9]+/, '').charAt(0).toUpperCase();
  return /[A-Z0-9]/.test(first) ? first : '#';
}

function sortKey(term: GlossaryTerm): string {
  return term.term.replace(/^[^A-Za-z0-9]+/, '').toLowerCase();
}

interface LetterGroup {
  letter: string;
  terms: GlossaryTerm[];
}

function groupAlphabetically(terms: readonly GlossaryTerm[]): LetterGroup[] {
  const sorted = [...terms].sort((a, b) => sortKey(a).localeCompare(sortKey(b)));
  const groups = new Map<string, GlossaryTerm[]>();
  for (const term of sorted) {
    const key = groupKey(term);
    const list = groups.get(key) ?? [];
    list.push(term);
    groups.set(key, list);
  }
  return [...groups.entries()].map(([letter, list]) => ({ letter, terms: list }));
}

const GROUPS = groupAlphabetically(glossary);
const TERMS_BY_ID = new Map(glossary.map((term) => [term.id, term]));

export function GlossaryPage() {
  return (
    <div className={styles.page}>
      <PageMeta
        title="Glossary"
        description="Definitions of the terms used across the four AI Agent Factory projects, with the meaning each project gives a term where the meanings differ: MCP Gateway, Registry, LiteLLM, Cedar, PrivateLink, Fast Path, self-service and more."
      />
      <PageHeader
        eyebrow="Concepts"
        title="Glossary"
        lead="Terms used across the four projects and the Amazon Bedrock AgentCore services they build on, with the meaning each project gives a term where the meanings differ."
      />

      <div className="container">
        <ConceptsNav />

        <p className={styles.intro}>
          The four projects were written by different teams at different times, so the same word can name different
          things. MCP Gateway names several products. Registry has more than one implementation. LiteLLM plays more
          than one role. Self-service is a product in one folder and a way of running the workshop in another. Where a
          project uses a term in its own way, that meaning is listed under the term with a link to the file it comes
          from.
        </p>

        <nav aria-label="Glossary sections" className={styles.letterNav}>
          <ul className={styles.letterList}>
            {GROUPS.map((group) => (
              <li key={group.letter}>
                <a href={`#letter-${group.letter}`} className={styles.letterLink}>
                  {group.letter}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        {GROUPS.map((group) => (
          <section key={group.letter} className={styles.letterSection} aria-labelledby={`letter-${group.letter}`}>
            <h2 id={`letter-${group.letter}`} className={styles.letterHeading}>
              {group.letter}
            </h2>
            <dl className={styles.termList}>
              {group.terms.map((term) => (
                <div key={term.id} className={styles.termEntry}>
                  <dt id={term.id} className={styles.term}>
                    {term.term}
                  </dt>
                  <dd className={styles.definition}>
                    <p>
                      {term.definition}
                      {term.source && (
                        <>
                          {' '}
                          (<SourceLink source={term.source}>source</SourceLink>)
                        </>
                      )}
                    </p>
                    {term.perProjectMeaning && term.perProjectMeaning.length > 0 && (
                      <ul className={styles.meaningList}>
                        {term.perProjectMeaning.map((meaning) => {
                          const project = getProjectById(meaning.projectId);
                          if (!project) return null;
                          return (
                            <li key={meaning.projectId} className={styles.meaning}>
                              <span className={styles.meaningProject}>
                                <StageBadge stage={project.stage} label={project.stageLabel} variant="outline" />
                                <Link to={projectPath(project.id)} className={styles.meaningProjectLink}>
                                  {project.shortName}
                                </Link>
                              </span>
                              <span className={styles.meaningText}>
                                {meaning.meaning} (<SourceLink source={meaning.source}>source</SourceLink>)
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                    {term.seeAlso && term.seeAlso.length > 0 && (
                      <p className={styles.seeAlso}>
                        <span className={styles.seeAlsoLabel}>See also: </span>
                        {term.seeAlso.map((id, index) => {
                          const related = TERMS_BY_ID.get(id);
                          if (!related) return null;
                          return (
                            <span key={id}>
                              {index > 0 && ', '}
                              <a href={`#${related.id}`}>{related.term}</a>
                            </span>
                          );
                        })}
                      </p>
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </div>
  );
}
