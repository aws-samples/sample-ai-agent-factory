import { Link } from 'react-router-dom';
import { FactsTable } from '../../components/FactsTable';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SourceLink } from '../../components/SourceLink';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts } from '../../content/facts';
import { PATHS, projectPath } from '../../paths';
import { JumpLinks, ProjectHeading, StartNav } from './shared';
import styles from './start.module.css';

export function PrerequisitesPage() {
  return (
    <>
      <PageMeta
        title="Prerequisites"
        description="What each AI Agent Factory project needs before its first deploy, at README parity, with the infrastructure tooling and validated regions it was tested in."
      />
      <PageHeader
        eyebrow="Start"
        title="Prerequisites"
        lead="What each of the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore needs before its first deploy, copied from its README, plus the infrastructure tooling and the regions it was validated in. The four lists differ enough that there is no single shared list."
      />
      <div className="container">
        <StartNav />
        <JumpLinks
          label="Prerequisites by project"
          lead="Jump to:"
          items={projects.map((project) => ({
            id: project.id,
            label: project.shortName,
          }))}
        />

        {projects.map((project) => {
          const facts = getFacts(project.id);
          return (
            <section
              key={project.id}
              id={project.id}
              className={styles.projectSection}
              aria-labelledby={`${project.id}-heading`}
            >
              <ProjectHeading project={project} id={`${project.id}-heading`} />
              <p className={styles.meta}>
                From the README: <SourceLink source={project.sources.prerequisites} />
              </p>
              <ul className={styles.bulletList}>
                {project.prerequisites.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              <FactsTable
                caption={`${project.shortName}: tooling and regions`}
                rows={[
                  { label: FACT_LABELS.iac, fact: facts.iac },
                  { label: FACT_LABELS.regions, fact: facts.regions },
                  {
                    label: FACT_LABELS.defaultRegion,
                    fact: facts.defaultRegion,
                  },
                ]}
              />
              <p className={styles.meta}>
                Next: <Link to={`${PATHS.start}#${project.id}`}>first ten minutes with {project.shortName}</Link> or the{' '}
                <Link to={projectPath(project.id)}>{project.shortName} project page</Link>.
              </p>
            </section>
          );
        })}
      </div>
    </>
  );
}
