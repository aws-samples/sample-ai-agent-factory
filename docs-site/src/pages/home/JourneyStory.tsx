import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { ArrowDown, ArrowRight, ArrowUpRight } from 'lucide-react';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { PageMeta } from '../../components/PageMeta';
import { projects, type JourneyStage } from '../../content/data';
import { getFacts } from '../../content/facts';
import { PATHS } from '../../paths';
import { StoryScene } from './StoryScenes';
import styles from './JourneyStory.module.css';

type StoryStage = JourneyStage | 'intro';
type ChapterStage = Exclude<StoryStage, 'intro'>;
type CSSVars = CSSProperties & { '--story-accent': string };

interface Chapter {
  stage: ChapterStage;
  eyebrow: string;
  title: string;
  description: string;
  cta: string;
  route: string;
  meta: string[];
  accent: string;
}

const CHAPTERS: Chapter[] = [
  {
    stage: 'learn',
    eyebrow: '01 / Learn · Workshop',
    title: 'Learn the platform patterns hands-on.',
    description: 'Understand the foundation and build the LLM Gateway, registries, and Tools Gateway module by module.',
    cta: 'Start with the Workshop',
    route: '/projects/workshop/',
    meta: ['Platform & ML engineers', 'First deploy'],
    accent: 'var(--stage-learn-on-dark)',
  },
  {
    stage: 'build',
    eyebrow: '02 / Build · Self-Service',
    title: 'Build and ship agents visually.',
    description: 'Build and ship agents fast on top of AgentCore, from a template to a working workflow on a visual canvas.',
    cta: 'Start with Self-Service',
    route: '/projects/self-service/',
    meta: ['AI/ML engineers', 'First deploy'],
    accent: 'var(--stage-build-on-dark)',
  },
  {
    stage: 'govern',
    eyebrow: '03 / Govern · MCP Gateway',
    title: 'Govern every tool call.',
    description: 'See Cedar ENFORCE, JWT authentication, interceptors, and a Guardrail on a live MCP endpoint, with per-tool-call authorization and audit.',
    cta: 'Start with the MCP Gateway',
    route: '/projects/mcp-gateway/',
    meta: ['Platform & security engineers', 'First deploy'],
    accent: 'var(--stage-govern-on-dark)',
  },
  {
    stage: 'scale',
    eyebrow: '04 / Scale · Blueprint',
    title: 'Evaluate the enterprise reference.',
    description: 'Evaluate a multi-account reference blueprint for building enterprise-scale infrastructure with shared platform capabilities.',
    cta: 'Explore the Blueprint',
    route: '/projects/blueprint/',
    meta: ['Platform & security engineers', 'Multi-account'],
    accent: 'var(--stage-scale-on-dark)',
  },
];

const CAPTIONS: Record<StoryStage, string> = {
  intro: 'Shared capabilities · One connected journey',
  learn: 'Learn · Platform patterns hands-on',
  build: 'Build · Agents on a visual canvas',
  govern: 'Govern · Identity + policy + audit',
  scale: 'Scale · Multi-account reference blueprint',
};

function projectName(stage: ChapterStage) {
  return projects.find((project) => project.stage === stage)?.name ?? '';
}

/** The hero canvas and the scroll story are independent: this fallback keeps SSR HTML lightweight. */
export function JourneyStory({ backdrop }: { backdrop?: React.ReactNode }) {
  const rootRef = useRef<HTMLElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const chapterRefs = useRef<Array<HTMLElement | null>>([]);
  const frameRef = useRef<number | null>(null);
  const [active, setActive] = useState<StoryStage>('intro');
  const [progress, setProgress] = useState(0);
  const [manualMotion, setManualMotion] = useState<boolean | null>(null);
  const [systemReduced, setSystemReduced] = useState(false);
  const reduced = manualMotion ?? systemReduced;
  const activeChapter = CHAPTERS.find((chapter) => chapter.stage === active);
  const accent = activeChapter?.accent ?? 'var(--stage-scale-on-dark)';

  useEffect(() => {
    setViewport(viewportRef.current);
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setSystemReduced(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    const update = () => {
      frameRef.current = null;
      const viewportHeight = window.innerHeight;
      const chapters = chapterRefs.current.filter((chapter): chapter is HTMLElement => chapter !== null);
      let selected: StoryStage = 'intro';
      let selectedProgress = 0;
      let selectedDistance = Number.POSITIVE_INFINITY;

      for (const chapter of chapters) {
        const rect = chapter.getBoundingClientRect();
        const distance = Math.abs(rect.top + rect.height / 2 - viewportHeight / 2);
        if (rect.top <= viewportHeight * .56 && rect.bottom > 0 && distance < selectedDistance) {
          selected = chapter.dataset.storyStage as StoryStage;
          selectedProgress = Math.min(1, Math.max(0, (viewportHeight * .5 - rect.top) / rect.height));
          selectedDistance = distance;
        }
      }

      // Once the story hands off to the existing project tiles, no sticky scene follows the reader.
      const storyRect = rootRef.current?.getBoundingClientRect();
      if (storyRect && storyRect.bottom <= 0) selected = 'scale';
      setActive(selected);
      setProgress(selectedProgress);
    };
    const schedule = () => {
      if (frameRef.current === null) frameRef.current = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule, { passive: true });
    return () => {
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
    };
  }, []);

  return (
    <section
      ref={rootRef}
      className={`${styles.story} on-dark`}
      style={{ '--story-accent': accent } as CSSVars}
      data-story-stage={active}
      data-story-motion={reduced ? 'static' : 'animated'}
      aria-label="The Agentic AI Factory journey"
    >
      <PageMeta
        title="Agentic AI Factory: enterprise agentic AI samples on AWS"
        description="Four complementary AWS samples for learning, building, governing and scaling agentic AI with Amazon Bedrock and Amazon Bedrock AgentCore, compared by stage, regions, deploy time and cost."
        titleIsFull
      />

      <div ref={viewportRef} className={styles.sticky} data-story-viewport><span className={styles.backgroundControl} />

        <div className={styles.storyCaption}>{CAPTIONS[active]}</div>
        <div className={`${styles.sceneWrap} ${active === 'build' || active === 'scale' ? styles.visualLeft : ''}`}>
          <StoryScene stage={active} progress={progress} />
        </div>

        <nav className={styles.progress} aria-label="Journey chapters" data-stage-journey data-size="md">
          {CHAPTERS.map((chapter) => (
            <a
              key={chapter.stage}
              className={styles.progressStep}
              href={`#story-${chapter.stage}`} data-stage={chapter.stage}
              data-active={active === chapter.stage}
              aria-current={active === chapter.stage ? 'step' : undefined}
            >
              <span aria-hidden="true" className={styles.progressDot} />
              <span className={styles.progressLabel}>{chapter.stage === 'learn' ? 'Learn' : chapter.stage === 'build' ? 'Build' : chapter.stage === 'govern' ? 'Govern' : 'Scale'}</span>
            </a>
          ))}
        </nav>

        <button
          type="button"
          className={styles.motionToggle}
          aria-pressed={reduced}
          aria-label={reduced ? 'Play story animations' : 'Pause story animations'}
          onClick={() => setManualMotion(!reduced)}
        >
          {reduced ? 'Play story' : 'Pause story'}
        </button>
      </div>

      <div className={styles.chapters}>
        <section className={`${styles.chapter} ${styles.intro}`} data-story-stage="intro" aria-label="Agentic AI Factory introduction">
          <div className={styles.chapterContent}>
            <PageHeader
              eyebrow="Enterprise agentic AI samples on AWS"
              className={styles.hero}
              title={<>Agentic AI<br /><span className={styles.titleAccent}>Factory</span></>}
              lead="Enterprise samples for building, governing, and operating agentic AI on AWS with Amazon Bedrock and Amazon Bedrock AgentCore. One journey. Four starting points. Scroll to explore."
              backdrop={backdrop}
              backdropContainer={viewport}
              actions={
                <>
                  <Button href="#story-learn" iconEnd={<ArrowDown size={17} />}>Begin the journey</Button>
                  <Button to={PATHS.whichProject} variant="secondary" iconEnd={<ArrowUpRight size={17} />}>Find your project</Button>
                </>
              }
              meta={<p className={styles.microNote}>Four self-contained samples · Learn · Build · Govern · Scale</p>}
            />
          </div>
        </section>

        {CHAPTERS.map((chapter, index) => (
          <section
            key={chapter.stage}
            id={`story-${chapter.stage}`}
            ref={(element) => { chapterRefs.current[index] = element; }}
            className={`${styles.chapter} ${index % 2 === 1 ? styles.alignRight : ''}`}
            data-story-stage={chapter.stage}
            aria-labelledby={`story-${chapter.stage}-title`}
          >
            <div className={styles.chapterContent}>
              <p className={styles.eyebrow}>{chapter.eyebrow}</p>
              <h2 id={`story-${chapter.stage}-title`}>{chapter.title}</h2>
              <p className={styles.chapterBody}><strong>{projectName(chapter.stage)}.</strong> {chapter.description}</p>
              <div className={styles.chapterActions}>
                <Button to={chapter.route} variant="secondary" iconEnd={<ArrowRight size={17} />}>{chapter.cta}</Button>
              </div>
              <div className={styles.facts}>
                {chapter.meta.map((fact) => <span key={fact}>{fact}</span>)}
              </div>
            </div>
          </section>
        ))}
      </div>

      {/* The full source-backed comparison stays directly after the story. */}
      <div className={styles.storyEnd} aria-hidden="true" />
      <span className={styles.srOnly}>The illustrations are conceptual, not live AWS resources. Deployment facts: {CHAPTERS.map((chapter) => `${chapter.eyebrow}: ${chapter.meta.join(', ')}${chapter.stage === 'scale' ? '' : `; ${getFacts(projects.find((project) => project.stage === chapter.stage)!.id).firstDeploy.value}`}`).join('; ')}.</span>
    </section>
  );
}
