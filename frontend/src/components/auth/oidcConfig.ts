import type { AuthProviderProps } from 'react-oidc-context';
import { WebStorageStateStore } from 'oidc-client-ts';
import { OIDC_AUTHORITY, OIDC_CLIENT_ID } from './oidcIdentity';

/**
 * OIDC config for Ava — public client on Keycloak realm "master".
 * Uses Authorization Code + PKCE. Tokens stored in sessionStorage (cleared on tab close).
 */
export const oidcConfig: AuthProviderProps = {
  authority: OIDC_AUTHORITY,
  client_id: OIDC_CLIENT_ID,
  redirect_uri: window.location.origin + '/',
  post_logout_redirect_uri: window.location.origin + '/',
  scope: 'openid profile email',
  automaticSilentRenew: true,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  onSigninCallback: () => {
    // Remove query params from URL after successful callback
    window.history.replaceState({}, document.title, window.location.pathname);
  },
};
