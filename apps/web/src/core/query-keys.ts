/** Central query-key helpers. The cache is cleared on every workspace switch, so keys need no workspace segment. */
export const workspaceKeys = {
  all: ['workspaces'] as const,
  list: () => [...workspaceKeys.all, 'list'] as const,
};
