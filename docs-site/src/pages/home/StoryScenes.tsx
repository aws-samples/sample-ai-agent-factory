import type { JourneyStage } from '../../content/data';
import styles from './JourneyStory.module.css';

const LABEL_PROPS = { textAnchor: 'middle' } as const;
const GLOW_PROPS = { filter: 'url(#story-glow)' } as const;

function SceneFrame({ children }: { children: React.ReactNode }) {
  return (
    <svg className={styles.scene} viewBox="0 0 1000 760" fill="none" focusable="false" aria-hidden="true">
      <defs>
        <radialGradient id="story-core-gradient" cx="0" cy="0" r="1" gradientTransform="translate(450 320) rotate(50) scale(170)">
          <stop className={styles.coreStopLight} />
          <stop offset=".38" className={styles.coreStopMid} />
          <stop offset="1" className={styles.coreStopDark} />
        </radialGradient>
        <linearGradient id="story-layer-gradient" x1="0" y1="0" x2="1" y2="1">
          <stop className={styles.layerStopLight} />
          <stop offset="1" className={styles.layerStopDark} />
        </linearGradient>
        <filter id="story-glow" x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="7" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
        <filter id="story-shadow" x="-30%" y="-30%" width="160%" height="180%">
          <feDropShadow dx="0" dy="12" stdDeviation="18" className={styles.shadowColor} floodOpacity=".45" />
        </filter>
      </defs>
      {children}
    </svg>
  );
}

function Card({ x, y, width, height, title, subtitle, detail, tone = 'default' }: {
  x: number; y: number; width: number; height: number; title: string; subtitle: string; detail?: string;
  tone?: 'default' | 'green' | 'violet';
}) {
  const center = x + width / 2;
  return (
    <g className={styles[`card-${tone}`]} filter="url(#story-shadow)">
      <rect className={styles.cardFill} x={x} y={y} width={width} height={height} rx="16" />
      <rect className={styles.cardStroke} x={x} y={y} width={width} height={height} rx="16" />
      <text x={center} y={y + height * .36} {...LABEL_PROPS} className={styles.cardKicker}>{subtitle}</text>
      <text x={center} y={y + height * .59} {...LABEL_PROPS} className={styles.cardTitle}>{title}</text>
      {detail && <text x={center} y={y + height * .79} {...LABEL_PROPS} className={styles.cardDetail}>{detail}</text>}
    </g>
  );
}

export function IntroScene() {
  return (
    <SceneFrame>
      <g filter="url(#story-shadow)">
        <rect className={styles.glass} x="280" y="40" width="600" height="152" rx="20" />
        <text x="314" y="76" className={styles.sceneLabel}>THE AGENTIC AI FACTORY</text>
        <circle className={styles.accentFill} cx="838" cy="70" r="5" {...GLOW_PROPS} />
        <text x="314" y="122" className={styles.glassTitle}>Shared capabilities.</text>
        <text x="314" y="155" className={styles.glassTitle}>Replaceable implementations.</text>
      </g>
      <path className={styles.accentStroke} d="M580 192V252L520 310" strokeWidth="1.5" />
      <circle className={styles.accentFill} cx="580" cy="217" r="5" {...GLOW_PROPS} />
      <g className={styles.dormantLinks} strokeDasharray="3 7"><path d="M520 310L270 368M520 310L770 368M490 385L360 548M550 385L680 548" /></g>
      <circle className={styles.coreAmbient} cx="520" cy="360" r="104" />
      <circle className={styles.core} cx="520" cy="360" r="68" {...GLOW_PROPS} />
      <path className={styles.coreMark} d="M494 360H546M520 334V386" strokeWidth="2" />
      <text x="520" y="454" {...LABEL_PROPS} className={styles.coreLabel}>AGENTCORE</text>
      <g className={styles.dormantNodes}>
        <circle cx="270" cy="368" r="33" /><circle cx="770" cy="368" r="33" />
        <circle cx="360" cy="568" r="33" /><circle cx="680" cy="568" r="33" /><circle cx="520" cy="630" r="33" />
      </g>
      <path className={styles.dormantLinks} d="M520 428V597" strokeDasharray="3 7" />
      <g {...LABEL_PROPS} className={styles.dormantLabels}>
        <text x="270" y="424">LLM GATEWAY</text><text x="770" y="424">TOOL GATEWAY</text>
        <text x="360" y="624">IDENTITY</text><text x="680" y="624">POLICY</text><text x="520" y="686">OBSERVABILITY</text>
      </g>
    </SceneFrame>
  );
}

export function LearnScene({ progress = 1 }: { progress?: number }) {
  const spread = (1 - progress) * 34;
  const layers = [
    { y: 130, title: 'Agent runtime', subtitle: 'REASON · PLAN · ACT', label: '01 / ACT', labelY: 260, tone: 'learn' },
    { y: 310, title: 'Gateways + identity', subtitle: 'MODELS · TOOLS · TRUST', label: '02 / CONNECT', labelY: 440, tone: 'default' },
    { y: 490, title: 'Policy + telemetry', subtitle: 'DECIDE · TRACE · EXPLAIN', label: '03 / CONTROL', labelY: 620, tone: 'violet' },
  ];
  return (
    <SceneFrame>
      <rect className={styles.infoPanel} x="294" y="20" width="412" height="65" rx="13" />
      <text x="320" y="47" className={styles.sceneLabel}>THE PLATFORM</text>
      <text x="320" y="70" className={styles.code}>runtime · gateways · identity · policy</text>
      <path className={styles.accentStroke} d="M500 85V126" strokeDasharray="3 5" />
      {layers.map((layer, index) => {
        const offset = index === 0 ? -spread : index === 2 ? spread : spread * .15;
        return (
          <g key={layer.title} className={styles[`layer-${layer.tone}`]} transform={`translate(0 ${offset})`}>
            <path className={styles.layerTop} d={`M500 ${layer.y}L770 ${layer.y + 125}L500 ${layer.y + 250}L230 ${layer.y + 125}Z`} />
            <path className={styles.layerEdge} d={`M230 ${layer.y + 125}V${layer.y + 151}L500 ${layer.y + 276}L770 ${layer.y + 151}V${layer.y + 125}L500 ${layer.y + 250}Z`} />
            <text x="500" y={layer.y + 124} {...LABEL_PROPS} className={styles.layerTitle}>{layer.title}</text>
            <text x="500" y={layer.y + 151} {...LABEL_PROPS} className={styles.cardDetail}>{layer.subtitle}</text>
            <text x="810" y={layer.labelY} className={styles.sceneLabel}>{layer.label}</text>
          </g>
        );
      })}
    </SceneFrame>
  );
}

export function BuildScene() {
  return (
    <SceneFrame>
      <rect className={styles.workflowPanel} x="315" y="35" width="370" height="64" rx="13" />
      <text x="339" y="61" className={styles.sceneLabel}>SHARED CAPABILITIES</text>
      <text x="339" y="84" className={styles.code}>runtime · gateways · identity · policy</text>
      <path className={styles.greenStrokeMuted} d="M500 99V152H175V310" strokeDasharray="4 6" />
      <path className={styles.greenStroke} d="M230 375H865" strokeWidth="2" />
      <path className={styles.greenStroke} d="M735 440V545" strokeWidth="1.5" />
      <circle className={styles.greenFill} cx="440" cy="375" r="5" {...GLOW_PROPS} />
      <Card x={55} y={310} width={175} height={130} title="Agent" subtitle="INPUT" detail="template" tone="green" />
      <Card x={340} y={285} width={200} height={180} title="AgentCore" subtitle="REASON + ACT" detail="runtime: managed" tone="green" />
      <Card x={645} y={310} width={180} height={130} title="Tool Gateway" subtitle="MCP · CATALOGUE" detail="connected" tone="green" />
      <Card x={865} y={310} width={120} height={130} title="Agent" subtitle="OUTPUT" detail="ready" tone="green" />
      <Card x={645} y={545} width={180} height={92} title="Registry" subtitle="AGENTS · SERVERS" tone="green" />
      <text x="500" y="708" {...LABEL_PROPS} className={styles.sceneLabel}>BUILD AND SHIP AGENTS ON A VISUAL CANVAS</text>
    </SceneFrame>
  );
}

export function GovernScene({ progress = 1 }: { progress?: number }) {
  const resolved = progress > .55;
  const traceOpacity = Math.min(1, Math.max(0, (progress - .5) * 4));
  return (
    <SceneFrame>
      <rect className={styles.governPanel} x="295" y="20" width="410" height="65" rx="13" />
      <text x="320" y="47" className={styles.sceneLabel}>PER-TOOL-CALL AUTHORIZATION</text>
      <text x="320" y="70" className={styles.code}>agent → tool call → policy decision</text>
      <path className={styles.violetStrokeMuted} d="M500 85V135H150V265" strokeDasharray="4 6" />
      <path className={styles.violetStroke} d="M245 375H910" strokeWidth="2" />
      <circle className={styles.violetFill} cx="425" cy="375" r="6" {...GLOW_PROPS} />
      <Card x={45} y={265} width={200} height={220} title="Tool call" subtitle="THE PLATFORM" detail="tools.invoke() · AGENT → MCP" tone="violet" />
      <Card x={315} y={265} width={220} height={220} title="JWT verified" subtitle="01 / IDENTITY" detail="JWT · IDENTITY PROPAGATION" tone="violet" />
      <g filter="url(#story-shadow)">
        <rect className={styles.policyCard} x="605" y="235" width="305" height="280" rx="18" />
        <text x="757" y="278" {...LABEL_PROPS} className={styles.sceneLabel}>02 / CEDAR POLICY</text>
        <text x="638" y="329" className={styles.codeMuted}>permit(</text>
        <text x="661" y="359" className={styles.code}>principal,</text>
        <text x="661" y="389" className={styles.code}>Action::&quot;invoke&quot;,</text>
        <text x="661" y="419" className={styles.code}>resource</text>
        <text x="638" y="449" className={styles.codeMuted}>) when {'{ principal.isAuthorized }'};</text>
        <rect className={resolved ? styles.permitBadge : styles.evaluatingBadge} x="662" y="468" width="191" height="31" rx="15.5" />
        <text x="757" y="489" {...LABEL_PROPS} className={resolved ? styles.permitText : styles.evaluatingText}>{resolved ? 'PERMITTED' : 'EVALUATING'}</text>
      </g>
      <g className={styles.trace} opacity={traceOpacity} transform={`translate(0 ${(1 - traceOpacity) * 20})`}>
        <rect x="120" y="575" width="760" height="104" rx="15" />
        <circle className={styles.greenFill} cx="153" cy="610" r="5" {...GLOW_PROPS} />
        <text x="174" y="615" className={styles.traceLabel}>AUTHORIZED → TOOL INVOKED → AUDIT RECORDED</text>
        <text x="153" y="651" className={styles.traceCode}>principal=agent  action=invoke  decision=permit</text>
      </g>
      <text x="500" y="723" {...LABEL_PROPS} className={styles.sceneLabel}>{resolved ? 'PER-TOOL-CALL AUTHORIZATION. A COMPLETE AUDIT TRAIL.' : 'IDENTITY AND POLICY TRAVEL WITH THE CALL'}</text>
    </SceneFrame>
  );
}

export function ScaleScene() {
  return (
    <SceneFrame>
      <rect className={styles.accountPanel} x="300" y="42" width="400" height="102" rx="17" filter="url(#story-shadow)" />
      <text x="500" y="79" {...LABEL_PROPS} className={styles.sceneLabel}>PLATFORM ACCOUNT</text>
      <text x="500" y="113" {...LABEL_PROPS} className={styles.accountTitle}>Gateways · identity · policy</text>
      <g className={styles.accountLinks} strokeWidth="1.5"><path d="M500 144V235L180 340M500 144V235V340M500 144V235L820 340" /></g>
      <circle className={styles.accentFill} cx="500" cy="235" r="6" {...GLOW_PROPS} />
      {[{ x: 35, label: 'ALPHA', detail: 'runtime=AgentCore' }, { x: 355, label: 'BETA', detail: 'tools=registry' }, { x: 675, label: 'GAMMA', detail: 'telemetry=connected' }].map(({ x, label, detail }) => (
        <g key={label} filter="url(#story-shadow)">
          <rect className={styles.workstreamCard} x={x} y="340" width="290" height="176" rx="17" />
          <text x={x + 145} y="378" {...LABEL_PROPS} className={styles.sceneLabel}>WORKSTREAM / {label}</text>
          <text x={x + 145} y="424" {...LABEL_PROPS} className={styles.workstreamTitle}>Agent workloads</text>
          <text x={x + 145} y="454" {...LABEL_PROPS} className={styles.code}>{detail}</text>
          <text x={x + 145} y="490" {...LABEL_PROPS} className={styles.inherited}>INHERITED CONTROLS ✓</text>
        </g>
      ))}
      <rect className={styles.contractPanel} x="155" y="585" width="690" height="82" rx="14" />
      <text x="500" y="618" {...LABEL_PROPS} className={styles.sceneLabel}>ONE CONTRACT · MANY INDEPENDENT TEAMS</text>
      <text x="500" y="647" {...LABEL_PROPS} className={styles.code}>identity=verified  policy=inherited  telemetry=connected</text>
    </SceneFrame>
  );
}

export function StoryScene({ stage, progress }: { stage: JourneyStage | 'intro'; progress: number }) {
  return (
    <div className={styles.scenes} data-scene={stage}>
      <div className={styles.sceneLayer} data-active={stage === 'intro'}><IntroScene /></div>
      <div className={styles.sceneLayer} data-active={stage === 'learn'}><LearnScene progress={progress} /></div>
      <div className={styles.sceneLayer} data-active={stage === 'build'}><BuildScene /></div>
      <div className={styles.sceneLayer} data-active={stage === 'govern'}><GovernScene progress={progress} /></div>
      <div className={styles.sceneLayer} data-active={stage === 'scale'}><ScaleScene /></div>
    </div>
  );
}
