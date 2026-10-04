import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// jsdom implements neither scrolling API; RouteChange and the skip link call both.
Object.defineProperty(window, 'scrollTo', { value: vi.fn(), writable: true, configurable: true });
Object.defineProperty(Element.prototype, 'scrollIntoView', { value: vi.fn(), writable: true, configurable: true });
