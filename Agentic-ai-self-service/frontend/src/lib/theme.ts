/**
 * Theme system — dark (default, neon) or light.
 *
 * Sets `data-theme` on <html>; index.css reads it (:root = dark, [data-theme=light]
 * overrides). Persists to localStorage. Applied pre-React (see applyStoredTheme,
 * called from main.tsx) to avoid a flash of the wrong theme.
 */
import { useSyncExternalStore } from 'react';

export type Theme = 'dark' | 'light';
const KEY = 'acf-theme';
const listeners = new Set<() => void>();

export function getStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch { /* ignore */ }
  return 'dark'; // dark neon is the default
}

function apply(theme: Theme) {
  const root = document.documentElement;
  // Dark is the base (:root); only set the attribute for light.
  if (theme === 'light') root.setAttribute('data-theme', 'light');
  else root.removeAttribute('data-theme');
}

/** Call once before React renders to prevent a flash. */
export function applyStoredTheme() {
  apply(getStoredTheme());
}

export function setTheme(theme: Theme) {
  try { localStorage.setItem(KEY, theme); } catch { /* ignore */ }
  apply(theme);
  listeners.forEach((l) => l());
}

export function toggleTheme() {
  setTheme(getStoredTheme() === 'dark' ? 'light' : 'dark');
}

/** React hook: current theme, re-renders on change. */
export function useTheme(): Theme {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getStoredTheme,
    () => 'dark',
  );
}
