import { useEffect, useRef } from 'react';
import { useAuth } from 'react-oidc-context';
import type { PropsWithChildren } from 'react';

/**
 * ⚠ AUTO-LOGIN — pourquoi ce comportement, et pourquoi il a fallu l'ajouter.
 *
 * Ava est derrière DEUX SSO empilés : Cloudflare Access (silencieux, car son
 * fournisseur d'identité EST Keycloak) puis le SSO propre à Ava. Sans auto-login,
 * l'utilisateur franchit Access sans s'en rendre compte… et tombe sur un écran
 * « SE CONNECTER » qui redirige vers le MÊME Keycloak où il vient de s'authentifier.
 * Un clic pour rien, à chaque visite — et l'impression que la connexion a échoué.
 *
 * C'est exactement ce que l'infrastructure a déjà réglé pour Grafana
 * (`GF_AUTH_GENERIC_OAUTH_AUTO_LOGIN=true`), avec la même contrepartie obligatoire :
 * un break-glass. D'où les deux gardes ci-dessous.
 */

/** ⚠ BREAK-GLASS. Si Keycloak tombe ou qu'une session est corrompue, l'auto-login
 *  renverrait en boucle vers un fournisseur en panne, sans aucun moyen de reprendre la
 *  main. `?noauto` rend le bouton — c'est le pendant du `/login?disableAutoLogin` de
 *  Grafana, et il doit exister AVANT d'en avoir besoin. */
function autoLoginDesactive(): boolean {
  try {
    return new URLSearchParams(window.location.search).has('noauto');
  } catch {
    return false;
  }
}

/** ⚠ ANTI-BOUCLE, et c'est le garde-fou qui compte vraiment.
 *  Si la redirection revient sans authentifier (session Keycloak expirée, cookie
 *  bloqué, `prompt=none` refusé), un auto-login inconditionnel repartirait aussitôt :
 *  la page clignoterait indéfiniment entre Ava et Keycloak, sans jamais afficher
 *  d'erreur. On n'essaie donc qu'une fois par fenêtre de 30 s ; passé cet essai,
 *  l'écran manuel reprend la main et l'utilisateur voit ce qui se passe. */
const CLE_TENTATIVE = 'ava.autologin.dernier';
const FENETRE_MS = 30_000;

function tentativeRecente(): boolean {
  try {
    const t = Number(sessionStorage.getItem(CLE_TENTATIVE) || 0);
    return t > 0 && Date.now() - t < FENETRE_MS;
  } catch {
    return true; // sessionStorage indisponible → on ne tente pas, on montre le bouton
  }
}

function marquerTentative(): void {
  try {
    sessionStorage.setItem(CLE_TENTATIVE, String(Date.now()));
  } catch {
    /* navigation privée : le bouton reste, c'est le repli correct */
  }
}

/**
 * Guard wrapper: renders the GitS-themed login screen while the user is not authenticated,
 * then forwards children once signed-in via Keycloak (OIDC authorization code + PKCE).
 */
export function LoginGate({ children }: PropsWithChildren) {
  const auth = useAuth();
  const lance = useRef(false);

  useEffect(() => {
    if (auth.isLoading || auth.isAuthenticated || auth.error) return;
    if (lance.current || autoLoginDesactive() || tentativeRecente()) return;
    lance.current = true;
    marquerTentative();
    void auth.signinRedirect();
  }, [auth.isLoading, auth.isAuthenticated, auth.error, auth]);

  if (auth.isLoading) {
    return <Screen title="AUTHENTICATING" subtitle="... sync with keycloak ..." />;
  }

  if (auth.error) {
    return (
      <Screen
        title="AUTH ERROR"
        subtitle={auth.error.message}
        accent="#ff6b8a"
        cta={{
          label: 'RETRY',
          onClick: () => { auth.removeUser(); auth.signinRedirect(); },
        }}
      />
    );
  }

  if (!auth.isAuthenticated) {
    // ⚠ On n'arrive ici QUE si l'auto-login est désactivé, a déjà été tenté sans
    //   succès, ou que la redirection est en cours. Le bouton reste donc le filet —
    //   jamais le chemin normal.
    return (
      <Screen
        title="AVA"
        subtitle="COGNITIVE INTERFACE // AUTHENTIFICATION REQUISE"
        cta={{
          label: 'SE CONNECTER',
          onClick: () => auth.signinRedirect(),
        }}
      />
    );
  }

  return <>{children}</>;
}

interface ScreenProps {
  title: string;
  subtitle: string;
  accent?: string;
  cta?: { label: string; onClick: () => void };
}

function Screen({ title, subtitle, accent = '#51a4de', cta }: ScreenProps) {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: '#000',
        color: '#b8cfe0',
        fontFamily: "'JetBrains Mono', 'Space Mono', 'Courier New', monospace",
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '2.2rem',
        overflow: 'hidden',
      }}
    >
      {/* scanlines */}
      <div style={{
        position: 'fixed', inset: 0,
        background: 'repeating-linear-gradient(0deg, rgba(81,164,222,0.03) 0px, rgba(81,164,222,0.03) 1px, transparent 1px, transparent 3px)',
        pointerEvents: 'none',
      }} />
      {/* vignette */}
      <div style={{
        position: 'fixed', inset: 0,
        background: 'radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.8) 100%)',
        pointerEvents: 'none',
      }} />

      <div style={{ textAlign: 'center', zIndex: 2 }}>
        <div
          style={{
            fontSize: 64,
            fontWeight: 700,
            letterSpacing: '0.4em',
            color: accent,
            textShadow: `0 0 36px ${accent}55`,
          }}
        >
          {title}
        </div>
        <div
          style={{
            marginTop: 12,
            fontSize: 10,
            letterSpacing: '0.3em',
            color: '#5a7a95',
            textTransform: 'uppercase',
          }}
        >
          {subtitle}
        </div>
      </div>

      {cta && (
        <button
          onClick={cta.onClick}
          style={{
            background: 'rgba(81,164,222,0.08)',
            border: `1px solid ${accent}`,
            color: accent,
            fontFamily: 'inherit',
            fontSize: 12,
            letterSpacing: '0.4em',
            padding: '14px 30px',
            cursor: 'pointer',
            textTransform: 'uppercase',
            zIndex: 2,
            transition: 'all 0.15s',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'rgba(81,164,222,0.2)';
            e.currentTarget.style.boxShadow = `0 0 24px ${accent}55`;
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'rgba(81,164,222,0.08)';
            e.currentTarget.style.boxShadow = 'none';
          }}
        >
          ▸ {cta.label}
        </button>
      )}

      <div style={{
        position: 'fixed',
        bottom: 28, left: '50%',
        transform: 'translateX(-50%)',
        fontFamily: "'MS Gothic', monospace",
        fontSize: 10,
        color: '#2a5a7a',
        opacity: 0.4,
        letterSpacing: '0.3em',
      }}>
        アヴァ // 認証システム
      </div>
    </div>
  );
}
