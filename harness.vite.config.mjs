/**
 * Build config for the ConceptGraph test harness (.harness/).
 *
 * The harness is a dev-only page that mounts the real ConceptGraph and
 * Inspector against a fixed 48-concept graph shaped like GET /api/graph, so
 * verify_graph.mjs can drive them without a backend or a Supabase session.
 * It is not part of the app: `npm run build` only ever reads index.html.
 *
 *   npx vite build --config harness.vite.config.mjs
 *   npx serve .harness-dist        (or any static server)
 *   node verify_graph.mjs http://localhost:PORT/.harness/index.html
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  root,
  plugins: [react()],
  build: {
    outDir: resolve(root, '.harness-dist'),
    emptyOutDir: true,
    rollupOptions: { input: resolve(root, '.harness/index.html') },
  },
})
