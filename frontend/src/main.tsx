import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { AuthProvider } from 'react-oidc-context';
import { oidcConfig } from './components/auth/oidcConfig';
import { ErrorBoundary } from './components/ErrorBoundary';
import App from './App';
import { initApiBase } from './lib/api';
import { initAnalytics } from './lib/analytics';
import './index.css';

// Same-origin API auth: prepend Authorization: Bearer <key> to /v1/* and /api/*
// fetches when a build-time key is provided (VITE_OJ_API_KEY). The OpenJarvis
// daemon middleware refuses unauthenticated /v1/* and /api/* calls when
// OPENJARVIS_API_KEY is set server-side.
const _OJ_API_KEY = import.meta.env.VITE_OJ_API_KEY ?? '';
if (_OJ_API_KEY) {
  const _origFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
        ? input.href
        : input.url;
    const isApiPath =
      url.startsWith('/v1/') ||
      url.startsWith('/api/') ||
      /^https?:\/\/[^/]+\/(v1|api)\//.test(url);
    if (!isApiPath) return _origFetch(input, init);
    const baseHeaders =
      init?.headers ?? (input instanceof Request ? input.headers : undefined);
    const headers = new Headers(baseHeaders);
    if (!headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${_OJ_API_KEY}`);
    }
    return _origFetch(input, { ...init, headers });
  };
}

function applyTheme() {
  try {
    const raw = localStorage.getItem('openjarvis-settings');
    const settings = raw ? JSON.parse(raw) : {};
    const theme = settings.theme || 'system';
    if (theme === 'dark') {
      document.documentElement.classList.add('dark');
      document.documentElement.classList.remove('light');
    } else if (theme === 'light') {
      document.documentElement.classList.add('light');
      document.documentElement.classList.remove('dark');
    }
  } catch { /* use system default */ }
}

applyTheme();

// Fetch the API base URL from the Tauri backend before rendering.
// This ensures JARVIS_PORT is defined in one place (the Rust backend).
// In non-Tauri environments this is a no-op.
initApiBase().finally(() => {
  // Kick off analytics init in the background — it's never awaited so
  // a slow/failed identity fetch never delays UI render.
  void initAnalytics();

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ErrorBoundary>
        <BrowserRouter>
          <AuthProvider {...oidcConfig}>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </ErrorBoundary>
    </StrictMode>,
  );
});
