import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  base: '/sample-ai-agent-factory/',
  publicDir: path.resolve(__dirname, '../assets'),
  build: {
    sourcemap: false,
  },
});
