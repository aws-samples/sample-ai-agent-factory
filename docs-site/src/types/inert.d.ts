import 'react';

/**
 * React 18 does not know the `inert` attribute. Passing the empty string makes
 * React emit `inert=""`, which browsers treat as the boolean attribute.
 * Usage: `{...(open ? { inert: '' } : {})}`.
 */
declare module 'react' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface HTMLAttributes<T> {
    inert?: '' | undefined;
  }
}
