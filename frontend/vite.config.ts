import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Ava',
        short_name: 'Ava',
        description: 'Ava - assistant IA personnel vocal FR',
        theme_color: '#161618',
        background_color: '#161618',
        display: 'standalone',
        icons: [
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        // ⚠ `html` A ÉTÉ RETIRÉ DE CETTE LISTE (2026-08-03) — et c'est la correction qui
        //   fait que les déploiements arrivent enfin sur les téléphones.
        //
        //   Le `runtimeCaching` ci-dessous existait déjà, avec le commentaire « NetworkFirst
        //   for HTML so a new deploy is picked up immediately ». Il était INOPÉRANT :
        //   `index.html` étant précaché par ce glob, Workbox sert les requêtes de NAVIGATION
        //   depuis le precache (`navigateFallback`), qui a la priorité — la règle
        //   `destination === 'document'` n'était jamais atteinte. Un réglage qui affirme son
        //   intention en commentaire et ne s'applique jamais : exactement la classe de défaut
        //   que ce projet documente ailleurs (options watchman acceptées puis ignorées,
        //   `max_chunk_size` de Nextcloud écrit dans la mauvaise clé).
        //
        //   SYMPTÔME VÉCU : le serveur servait bien le nouveau bundle (vérifié par empreinte
        //   sur :8000 ET sur le relay :8080), et le téléphone affichait l'ancienne interface
        //   après rechargement. On soupçonne alors le déploiement, puis Cloudflare — alors que
        //   ni l'un ni l'autre n'étaient en cause. Un déploiement qui n'atteint personne se lit
        //   exactement comme un déploiement réussi.
        //
        //   ⚠ CE QUE ÇA COÛTE, ASSUMÉ : l'application ne démarre plus hors ligne, puisque son
        //   HTML n'est plus en cache d'avance. C'est sans conséquence ici — Ava ne sait rien
        //   faire sans son daemon : une coquille qui s'ouvre pour afficher une erreur réseau
        //   n'a aucune valeur, et elle coûtait la certitude de servir la version déployée.
        globPatterns: ['**/*.{js,css,ico,png,svg,woff2}'],
        // ⚠ INDISSOCIABLE DU POINT CI-DESSUS. `vite-plugin-pwa` pose par défaut
        //   `navigateFallback: 'index.html'`, ce qui enregistre une `NavigationRoute` liée
        //   au PRECACHE (`createHandlerBoundToURL`). Retirer `html` du glob sans neutraliser
        //   ceci laisserait le service worker chercher un fichier qui n'y est plus — il ne
        //   sert alors plus rien du tout, ce qui est PIRE que le défaut d'origine : on
        //   passerait d'« ancienne version affichée » à « page blanche ».
        //   Les navigations sont prises en charge par le `runtimeCaching` NetworkFirst plus
        //   bas ; le serveur, lui, renvoie déjà `index.html` pour les routes de la SPA.
        navigateFallback: null,
        navigateFallbackDenylist: [/^\/v1\//, /^\/api\//, /^\/health/, /^\/dashboard/, /^\/onnx\//, /^\/assets\//, /\.wasm$/, /\.mjs$/],
        skipWaiting: true,
        clientsClaim: true,
        cleanupOutdatedCaches: true,
        // NetworkFirst for HTML so a new deploy is picked up immediately.
        // ⚠ Cette règle n'est ATTEINTE que parce que `html` a quitté `globPatterns` ci-dessus.
        //   Le remettre la rendrait silencieusement inerte à nouveau.
        runtimeCaching: [
          {
            urlPattern: ({ request }) => request.destination === 'document',
            handler: 'NetworkFirst',
            options: { cacheName: 'ava-html', networkTimeoutSeconds: 3 },
          },
        ],
      },
    }),
  ],
  build: {
    outDir: '../src/openjarvis/server/static',
    emptyOutDir: true,
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom'],
          markdown: ['react-markdown', 'rehype-highlight', 'remark-gfm'],
          charts: ['recharts'],
          router: ['react-router'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': process.env.VITE_API_URL || 'http://localhost:8000',
      '/health': process.env.VITE_API_URL || 'http://localhost:8000',
    },
  },
});
