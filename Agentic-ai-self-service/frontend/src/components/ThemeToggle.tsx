/**
 * ThemeToggle — sun/moon switch for light/dark. Lives in the header.
 */
import { m } from 'motion/react';
import { useTheme, toggleTheme } from '../lib/theme';
import { spring } from '../lib/motion';

export function ThemeToggle() {
  const theme = useTheme();
  const isDark = theme === 'dark';

  return (
    <m.button
      onClick={toggleTheme}
      whileTap={{ scale: 0.9 }}
      transition={spring.snappy}
      className="no-darkmap relative flex items-center justify-center w-8 h-8 rounded-md"
      style={{
        color: 'rgba(255,255,255,0.7)',
        border: '1px solid var(--color-border)',
        background: 'rgba(255,255,255,0.04)',
      }}
      title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      data-testid="theme-toggle"
    >
      {isDark ? (
        // moon
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      ) : (
        // sun
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      )}
    </m.button>
  );
}
