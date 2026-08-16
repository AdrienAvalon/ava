import { describe, expect, it } from 'vitest';

import {
  dataSourcesPrincipalKey,
  managedAgentLoadStateForError,
  shouldAutoCreateManagedAgent,
} from './DataSourcesPage';

describe('principal-scoped data sources', () => {
  it('uses a distinct remount key for each verified subject', () => {
    expect(dataSourcesPrincipalKey('https://issuer', 'principal-a')).toBe('https://issuer:principal-a');
    expect(dataSourcesPrincipalKey('https://issuer', 'principal-b')).toBe('https://issuer:principal-b');
    expect(dataSourcesPrincipalKey(null, 'principal-a')).toBeNull();
    expect(dataSourcesPrincipalKey('https://issuer', '   ')).toBeNull();
  });

  it('classifies a managed-agent 401 as unauthorized', () => {
    expect(managedAgentLoadStateForError(new Error('Failed: 401'))).toBe('unauthorized');
    expect(managedAgentLoadStateForError(new Error('Failed: 503'))).toBe('error');
  });

  it('never auto-creates after an unauthorized response or a failed attempt', () => {
    const base = {
      activeTab: 'messaging' as const,
      hasAgent: false,
      creatingAgent: false,
      creationAttempted: false,
    };

    expect(shouldAutoCreateManagedAgent({ ...base, loadState: 'unauthorized' })).toBe(false);
    expect(shouldAutoCreateManagedAgent({ ...base, loadState: 'ready' })).toBe(true);
    expect(shouldAutoCreateManagedAgent({
      ...base,
      loadState: 'ready',
      creationAttempted: true,
    })).toBe(false);
  });
});
