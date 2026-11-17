/** Central query-key helpers. The cache is cleared on every workspace switch, so keys need no workspace segment. */
export const workspaceKeys = {
  all: ['workspaces'] as const,
  list: () => [...workspaceKeys.all, 'list'] as const,
  /** One translated resource revision: [workspace, generation, 'translation', type, id, revision, settings revision]. */
  translation: (workspaceId: string | null, generation: number, type: string, id: string, revision: string, settingsRevision: number) =>
    [workspaceId, generation, 'translation', type, id, revision, settingsRevision] as const,
};
