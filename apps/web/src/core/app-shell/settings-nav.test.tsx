import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsPanel } from "@/core/app-shell/settings-nav";
import type { NavigationDestination } from "@/core/module-registry";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/core/guarded-navigation", () => ({ useGuardedNavigation: () => ({ navigate: vi.fn() }) }));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children: React.ReactNode }) => <div>{children}</div>, SelectContent: () => null, SelectItem: () => null,
  SelectTrigger: () => null, SelectValue: () => null,
}));

const link = (id: string, key: string): NavigationDestination => ({ id, href: `/settings/${id}`, messageKey: key } as NavigationDestination);

describe("SettingsPanel groups", () => {
  it("splits links into Data & AI and Workspace groups", () => {
    const links = [link("workspace-members", "m"), link("web-search-settings", "ws"), link("ai-router", "ai"), link("data-sources", "ds"), link("n8n-settings", "n8n")];
    render(<SettingsPanel links={links} pathname="/settings/ai-router">x</SettingsPanel>);
    const dataAi = screen.getByRole("group", { name: "settingsGroupDataAi" });
    const workspace = screen.getByRole("group", { name: "settingsGroupWorkspace" });
    expect(within(dataAi).getAllByRole("link").map((a) => a.textContent)).toEqual(["ds", "ai", "ws"]);
    expect(within(workspace).getAllByRole("link").map((a) => a.textContent)).toEqual(["m", "n8n"]);
  });

  it("omits a group with no visible links", () => {
    render(<SettingsPanel links={[link("translation-settings", "tr")]} pathname="/settings/x">x</SettingsPanel>);
    expect(screen.queryByRole("group", { name: "settingsGroupWorkspace" })).toBeNull();
  });
});
