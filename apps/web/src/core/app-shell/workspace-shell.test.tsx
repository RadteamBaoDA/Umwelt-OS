import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const replace = vi.fn();
const apiRequest = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }), usePathname: () => "/dashboard" }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/core/api", async () => {
  class ApiError extends Error {
    constructor(readonly status: number, message: string) { super(message); }
  }
  return { ApiError, apiRequest: (...args: unknown[]) => apiRequest(...args), csrfHeaders: () => ({}), retrySessionConflict: () => false };
});
vi.mock("@/core/query-provider", () => ({
  useDisplayPreferences: () => ({
    authGeneration: 0, confirmedPreferences: null, confirmPreferences: vi.fn(), endAuthSession: vi.fn(), isCurrentGeneration: () => true,
  }),
}));
vi.mock("@/core/realtime-provider", () => ({ useRealtime: () => ({ status: "open" }) }));
vi.mock("@/core/workspace-context", () => ({ useWorkspace: () => ({ selection: { id: "w1", role: "owner", revision: 1 } }) }));
vi.mock("@/core/app-shell/chat-controller", () => ({ useChatController: () => ({}) }));
vi.mock("@/core/command-palette", () => ({ CommandPalette: () => null }));
vi.mock("@/core/app-shell/connection-footer", () => ({ ConnectionFooter: () => null }));
vi.mock("@/core/app-shell/settings-nav", () => ({ SettingsPanel: () => null, SourcesSubNav: () => null, groupActive: () => false }));
vi.mock("@/core/app-shell/workspace-switcher", () => ({ WorkspaceSwitcher: () => null }));
vi.mock("@/modules/account/change-password-dialog", () => ({ ChangePasswordDialog: () => null }));
vi.mock("@/modules/account/google-link", () => ({ GoogleLink: () => null }));
vi.mock("@/modules/account/account-deletion", () => ({ AccountDeletionSection: () => null }));
vi.mock("@/modules/legal/legal-links", () => ({ LegalLinks: () => null }));
vi.mock("@/modules/account/preferences-dialog", () => ({ PreferencesDialog: () => null }));
vi.mock("@/modules/chat/chat-drawer", () => ({ ChatDrawer: () => null }));
vi.mock("@/modules/notifications/notification-bell", () => ({ NotificationBell: () => null }));
vi.mock("@/modules/account/user-menu", () => ({
  UserMenu: ({ onSignOut }: { onSignOut: () => void }) => <button onClick={onSignOut}>sign-out</button>,
}));

import { ApiError } from "@/core/api";
import { WorkspaceShell } from "@/core/app-shell/workspace-shell";

/** Renders a shell under a brand-new query client, like the auth-keyed remount after sign-out. */
function mountShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><WorkspaceShell>content</WorkspaceShell></QueryClientProvider>);
}

describe("WorkspaceShell sign-out", () => {
  beforeEach(() => { replace.mockClear(); apiRequest.mockReset(); });

  it("routes a post-logout 401 on the remounted shell to /login, not ?reason=expired", async () => {
    apiRequest.mockImplementation(async (path: string) => {
      if (path === "/api/v1/auth/session") return { authenticated: true, csrfToken: "t", isInstanceAdmin: false };
      return undefined;
    });
    const first = mountShell();
    await userEvent.click(await screen.findByText("sign-out"));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    first.unmount();

    // Keyed remount: fresh refs, server already revoked the session.
    apiRequest.mockImplementation(async () => { throw new ApiError(401, "expired"); });
    replace.mockClear();
    mountShell();
    await vi.waitFor(() => expect(replace).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
    expect(replace).not.toHaveBeenCalledWith("/login?reason=expired");
  });
});
