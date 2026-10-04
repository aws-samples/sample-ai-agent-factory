import { Buffer } from 'node:buffer'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

function buildConfigMetadata(env: Record<string, string>) {
  // These four values are public client configuration and already exist in the
  // generated JavaScript. The base64url wrapper is for unambiguous HTML
  // transport, not secrecy. A post-build verifier decodes this manifest before
  // deploy so an unauthenticated/local build cannot be uploaded by mistake.
  const content = Buffer.from(JSON.stringify({
    apiBaseUrl: env.VITE_API_BASE_URL ?? '',
    awsRegion: env.VITE_AWS_REGION ?? '',
    userPoolId: env.VITE_COGNITO_USER_POOL_ID ?? '',
    userPoolClientId: env.VITE_COGNITO_CLIENT_ID ?? '',
  })).toString('base64url')

  return {
    name: 'agentcore-build-config-metadata',
    transformIndexHtml() {
      return [{
        tag: 'meta',
        attrs: {
          name: 'agentcore-build-config',
          content,
        },
        injectTo: 'head' as const,
      }]
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')

  return {
    plugins: [buildConfigMetadata(env), react(), tailwindcss()],
    build: {
      rollupOptions: {
        output: {
          // Keep the initial application chunk small enough to parse promptly on
          // ordinary enterprise laptops, while giving slow-changing framework
          // code stable cache boundaries. These packages dominate the bundle and
          // are independent of the feature code users iterate on.
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (
              id.includes('/aws-amplify/') ||
              id.includes('/@aws-amplify/') ||
              id.includes('/amazon-cognito-identity-js/')
            ) return 'aws-vendor'
            if (id.includes('/@xyflow/')) return 'canvas-vendor'
            if (id.includes('/motion/')) return 'motion-vendor'
            if (
              id.includes('/react/') ||
              id.includes('/react-dom/') ||
              id.includes('/scheduler/')
            ) return 'react-vendor'
            return undefined
          },
        },
      },
    },
    server: {
      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
