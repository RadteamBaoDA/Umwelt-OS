import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { destinationForRole, detailDestinations, mainNavigation } from "@/core/module-registry";
import { WorkspaceSwitcher } from "@/core/app-shell/workspace-switcher";
import { workspacesMessages } from "@/core/messages/workspaces";

const select = vi.fn();
vi.mock("@/core/workspace-context", () => ({
  useWorkspace: () => ({
    selection: { id: "w1", role: "owner", revision: 1 },
    workspaces: [
      { id: "w1", name: "Mine", role: "owner" },
      { id: "w2", name: "Theirs", role: "member" },
    ],
    selectWorkspace: select,
  }),
}));

describe("member navigation", () => {
  it("shows only shared surfaces to a member and everything to an owner", () => {
    const ids = (role: "owner" | "member") =>
      [...mainNavigation, ...detailDestinations].filter((d) => destinationForRole(d, role)).map((d) => d.id);
    expect(ids("member")).toEqual(["dashboard", "chat", "settings", "documents", "search"]);
    expect(ids("owner")).toContain("entities");
  });
});

describe("WorkspaceSwitcher", () => {
  it("calls selectWorkspace with the chosen id", async () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={{ workspaces: workspacesMessages["en-us"] }}>
        <WorkspaceSwitcher />
      </NextIntlClientProvider>,
    );
    await userEvent.click(screen.getByRole("combobox", { name: "Workspace" }));
    await userEvent.click(await screen.findByRole("option", { name: /Theirs/ }));
    expect(select).toHaveBeenCalledWith("w2");
  });
});
