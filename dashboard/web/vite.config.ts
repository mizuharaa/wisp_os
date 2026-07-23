import path from "node:path"
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Served in production from dashboard/serve.py's explicit static allowlist
// at exactly this prefix (see dashboard/serve.py's _static_denied) — the
// emitted asset URLs must be root-absolute under this path regardless of
// what URL the HTML document itself was served from.
const DIST_BASE = "/dashboard/web/dist/"

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  // Only the production build needs the dist-prefixed base — the dev server
  // serves from its own root and is reached through the proxy below instead.
  base: command === "build" ? DIST_BASE : "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      // changeOrigin rewrites the Host header; serve.py's _origin_allowed()
      // also checks the Origin header against its own port (a same-origin
      // guard against arbitrary pages POSTing to this locally-bound admin
      // API), which the dev server's :5173 origin fails by construction.
      // Strip it so a proxied request reads as having no Origin header at
      // all -- the same treatment serve.py already gives non-browser CLI
      // clients -- rather than loosening that check on the backend itself.
      "/api": {
        target: "http://127.0.0.1:8817",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => proxyReq.removeHeader("origin"))
        },
      },
      "/tokens.css": "http://127.0.0.1:8817",
      "/state": "http://127.0.0.1:8817",
      "/skills/registry.json": "http://127.0.0.1:8817",
      "/hermes/solved.jsonl": "http://127.0.0.1:8817",
      "/memory/OBSIDIAN.md": "http://127.0.0.1:8817",
    },
  },
}))
