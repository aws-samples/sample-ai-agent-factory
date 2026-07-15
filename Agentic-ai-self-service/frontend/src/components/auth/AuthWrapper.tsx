/**
 * AuthWrapper - Login screen with MotionSites cinematic hero design.
 * Wraps AWS Amplify Authenticator with animated gradient background,
 * Instrument Serif italic accent, and liquid-glass badge.
 *
 * Implementation note: the Amplify `Authenticator`'s children render-prop is
 * ONLY invoked once `route === 'authenticated' | 'signOut'` — pre-auth it
 * renders its own <Router> UI. The hero must therefore live OUTSIDE the
 * Authenticator, gated on `authStatus` from `useAuthenticator`, with the
 * `<Authenticator>` mounted inside the hero layout. A nested
 * AuthenticatorProvider reuses the parent context (parentProviderVal ?? ...),
 * so wrapping in Authenticator.Provider is safe.
 */
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import { m } from 'motion/react';
import { AnimatedHeroBackground } from '../hero';
import { staggerContainer, fadeRise } from '../../lib/motion';
import type { ReactNode } from 'react';
interface AuthWrapperProps {
  children: ReactNode;
}
function AuthShell({ children }: AuthWrapperProps) {
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  // User is authenticated - render the app
  if (authStatus === 'authenticated') {
    return <>{children}</>;
  }
  // Login screen with cinematic hero (also shown while 'configuring'
  // so there is no flash of unstyled auth UI during the token check)
  return (
    <div className="relative w-screen h-screen overflow-hidden">
      {/* Animated gradient background */}
      <AnimatedHeroBackground />
      {/* Hero content - centered */}
      <div className="relative z-10 flex flex-col items-center justify-center h-full px-4 overflow-y-auto">
        {/* Orchestrated staggered entrance (framer-motion) */}
        <m.div
          className="flex flex-col items-center gap-6 max-w-2xl text-center"
          variants={staggerContainer(0.1, 0.1)}
          initial="hidden"
          animate="visible"
        >
          {/* Liquid-glass badge with a live "shipping" dot */}
          <m.div
            variants={fadeRise}
            className="rounded-full px-1.5 py-1.5"
            style={{ background: 'var(--glass-surface-glass, rgba(255,255,255,0.1))', backdropFilter: 'blur(8px)' }}
          >
            <div
              className="flex items-center gap-2 rounded-full px-3 py-1 text-sm font-medium tracking-tight"
              style={{ backgroundColor: 'rgba(255, 255, 255, 0.9)', color: '#171717' }}
            >
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-500 opacity-75" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
              </span>
              AgentCore Visual Platform
            </div>
          </m.div>
          {/* Hero headline - Instrument Serif italic + Barlow light */}
          <div className="flex flex-col gap-2">
            <m.h1
              variants={fadeRise}
              className="text-4xl sm:text-5xl md:text-6xl lg:text-7xl text-white leading-tight"
              style={{ fontFamily: 'var(--font-accent)', fontStyle: 'italic', fontWeight: 400 }}
            >
              Build Agents <span className="u-neon-text u-gradient-anim">Visually</span>
            </m.h1>
            <m.p
              variants={fadeRise}
              className="text-lg sm:text-xl md:text-2xl font-light tracking-tight"
              style={{ color: 'rgba(255, 255, 255, 0.75)' }}
            >
              Low-code workflow builder for AWS AgentCore
            </m.p>
          </div>
          {/* Amplify sign-in form, themed by the style block below */}
          <m.div variants={fadeRise} className="mt-8 w-full max-w-md">
            <Authenticator hideSignUp={true} />
          </m.div>
        </m.div>
      </div>
      <style>{`
        /* Style Amplify UI form to match MotionSites */
        [data-amplify-authenticator] {
          --amplify-colors-background-primary: rgba(255, 255, 255, 0.95);
          --amplify-colors-border-primary: rgba(255, 255, 255, 0.2);
          --amplify-radii-small: 2px;
          --amplify-radii-medium: 2px;
          --amplify-radii-large: 2px;
        }
        [data-amplify-authenticator] button[type="submit"] {
          background-color: var(--color-motion-off-white) !important;
          color: var(--color-motion-dark) !important;
          border-radius: var(--radius-motion) !important;
          transition: all 200ms cubic-bezier(0.22, 1, 0.36, 1) !important;
          font-weight: 500 !important;
        }
        [data-amplify-authenticator] button[type="submit"]:hover {
          background-color: #ffffff !important;
          transform: scale(1.01);
        }
        [data-amplify-authenticator] input {
          border-radius: var(--radius-motion) !important;
          transition: border-color 200ms ease !important;
        }
      `}</style>
    </div>
  );
}
export function AuthWrapper({ children }: AuthWrapperProps) {
  return (
    <Authenticator.Provider>
      <AuthShell>{children}</AuthShell>
    </Authenticator.Provider>
  );
}
