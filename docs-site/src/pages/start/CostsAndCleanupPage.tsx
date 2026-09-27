import { Link } from 'react-router-dom';
import { Callout } from '../../components/Callout';
import { CodeBlock } from '../../components/CodeBlock';
import { FactsTable } from '../../components/FactsTable';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { SourceLink } from '../../components/SourceLink';
import { projects } from '../../content/data';
import { FACT_LABELS, getFacts } from '../../content/facts';
import { getQuickstarts } from '../../content/quickstarts';
import { PATHS, projectPath } from '../../paths';
import { JumpLinks, ProjectHeading, SmallSource, StartNav } from './shared';
import styles from './start.module.css';

const ROOT_COSTS = { file: 'README.md', heading: 'Costs' };

export function CostsAndCleanupPage() {
  return (
    <>
      <PageMeta
        title="Costs and cleanup"
        description="What each AI Agent Factory project costs to run, as far as its README says, and how to remove everything afterwards, with source links."
      />
      <PageHeader
        eyebrow="Start"
        title="Costs and cleanup"
        lead="What each of the four samples for agentic AI on Amazon Bedrock and Amazon Bedrock AgentCore costs to run, as far as its documentation says, and how to remove everything afterwards. Where a project publishes no figure, this page says so instead of guessing."
      />
      <div className="container">
        <StartNav />

        <Callout kind="warning" title="Every project deploys real, billable AWS resources">
          <p>
            All four projects deploy real, billable AWS resources and every project invokes Amazon Bedrock models. Costs
            accrue while resources exist, whether or not you are using them. Tear down when you finish, using each
            project&apos;s cleanup commands below. <SmallSource source={ROOT_COSTS} context="the cost notice" />
          </p>
        </Callout>

        <JumpLinks
          label="Costs and cleanup by project"
          lead="Jump to:"
          items={projects.map((project) => ({
            id: project.id,
            label: project.shortName,
          }))}
        />

        {projects.map((project) => {
          const facts = getFacts(project.id);
          const quickstarts = getQuickstarts(project.id);
          return (
            <section
              key={project.id}
              id={project.id}
              className={styles.projectSection}
              aria-labelledby={`${project.id}-heading`}
            >
              <ProjectHeading project={project} id={`${project.id}-heading`} />
              <FactsTable
                caption={`${project.shortName}: cost and teardown`}
                rows={[
                  { label: FACT_LABELS.cost, fact: facts.cost },
                  { label: FACT_LABELS.teardown, fact: facts.teardown },
                ]}
              />
              {quickstarts.map((quickstart) => (
                <div key={quickstart.id} className={styles.subSection}>
                  <h3>{quickstarts.length > 1 ? `Teardown: ${quickstart.name}` : 'Teardown'}</h3>
                  <p>
                    {quickstart.teardown.text}{' '}
                    <SmallSource source={quickstart.teardown.source} context={`teardown: ${quickstart.name}`} />
                  </p>
                  {quickstart.teardown.command && <CodeBlock code={quickstart.teardown.command} language="bash" />}
                </div>
              ))}
              <p className={styles.meta}>
                More: <Link to={projectPath(project.id)}>{project.shortName} project page</Link>,{' '}
                <Link to={`${PATHS.start}#${project.id}`}>first ten minutes with {project.shortName}</Link>, and the{' '}
                <SourceLink source={facts.teardown.source ?? project.sources.features}>README</SourceLink>.
              </p>
            </section>
          );
        })}
      </div>
    </>
  );
}
