/**
 * AuthWrapper - Login screen with MotionSites cinematic hero design.
 * Wraps AWS Amplify Authenticator with animated gradient background,
 * Instrument Serif italic accent, and liquid-glass badge.
 */

import { Authenticator } from '@aws-amplify/ui-react';
import { AnimatedHeroBackground } from '../hero';
import type { ReactNode } from 'react';

interface AuthWrapperProps {
  children: ReactNode;
}

export function AuthWrapper({ children }: AuthWrapperProps) {
  return (
    <Authenticator hideSignUp={true}>
      {({ user }) => {
        // User is authenticated - render the app
        if (user) {
          return <>{children}</>;
        }

        // Login screen with cinematic hero
        return (
          <div className="relative w-screen h-screen overflow-hidden">
            {/* Animated gradient background */}
            <AnimatedHeroBackground />

            {/* Hero content - centered */}
            <div className="relative z-10 flex flex-col items-center justify-center h-full px-4">
              {/* Staggered fade-in content */}
              <div
                className="flex flex-col items-center gap-6 max-w-2xl text-center"
                style={{
                  animation: 'heroFadeIn 0.8s cubic-bezier(0.22, 1, 0.36, 1) forwards',
                }}
              >
                {/* Liquid-glass badge */}
                <div
                  className="rounded-full px-1.5 py-1.5 backdrop-blur-sm"
                  style={{
                    backgroundColor: 'rgba(255, 255, 255, 0.1)',
                  }}
                >
                  <div
                    className="rounded-full px-3 py-1 text-sm font-medium tracking-tight"
                    style={{
                      backgroundColor: 'rgba(255, 255, 255, 0.9)',
                      color: '#171717',
                    }}
                  >
                    AgentCore Visual Platform
                  </div>
                </div>

                {/* Hero headline - Instrument Serif italic + Barlow light */}
                <div className="flex flex-col gap-2">
                  <h1
                    className="text-4xl sm:text-5xl md:text-6xl lg:text-7xl text-white leading-tight"
                    style={{
                      fontFamily: 'var(--font-accent)',
                      fontStyle: 'italic',
                      fontWeight: 400,
                    }}
                  >
                    Build Agents Visually
                  </h1>
                  <p
                    className="text-lg sm:text-xl md:text-2xl font-light tracking-tight"
                    style={{
                      color: 'rgba(255, 255, 255, 0.75)',
                    }}
                  >
                    Low-code workflow builder for AWS AgentCore
                  </p>
                </div>

                {/* Auth form will be rendered by Authenticator internally */}
                <div className="mt-8 w-full max-w-md">
                  {/* Authenticator default UI renders here */}
                </div>
              </div>
            </div>

            <style>{`
              @keyframes heroFadeIn {
                from {
                  opacity: 0;
                  transform: translateY(12px);
                }
                to {
                  opacity: 1;
                  transform: translateY(0);
                }
              }

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
      }}
    </Authenticator>
  );
}
