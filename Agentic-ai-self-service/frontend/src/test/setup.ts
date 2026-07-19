import '@testing-library/jest-dom'

// jsdom doesn't implement scrollIntoView; components with autoscroll (chat,
// AI generator panels) call it in effects. Stub it so those render in tests.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
