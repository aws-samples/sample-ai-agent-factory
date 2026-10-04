// DeployButton, DeploymentModal and ErrorSummaryModal were removed: nothing
// imported them, and nothing imported this barrel either, so the whole family was
// unreachable from the running app. App.tsx renders AppHeader's Deploy button ->
// DeployPanel instead.
//
// They were not harmless. They held the only copies of the `deploy-button`,
// `deployment-modal` and `error-summary-modal` test ids — 3 of the 44 in the
// frontend, and the only 3 that match nothing in a real browser — so UI tests
// written against them waited on elements that are never in the DOM. They also
// carried a 393-line passing test suite for a component the product never renders.
export { DeployPanel, type DeployPanelProps, type DeployConnector, type CustomToolData } from './DeployPanel';
