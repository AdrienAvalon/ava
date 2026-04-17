import type { AuthProviderProps } from 'react-oidc-context';
import { WebStorageStateStore } from 'oidc-client-ts';

/**
 * OIDC config for Ava — public client on Keycloak realm "master".
 * Uses Authorization Code + PKCE. Tokens stored in sessionStorage (cleared on tab close).
 */
export const oidcConfig: AuthProviderProps = {
  authority: 'https://auth.avalon-network.com/realms/master',
  client_id: 'ava',
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
