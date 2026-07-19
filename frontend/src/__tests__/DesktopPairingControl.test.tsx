import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import type { AuthSession } from "../api/auth";
import type { ApiConfig } from "../api/core";
import DesktopPairingControl from "../features/desktopPairing/DesktopPairingControl";
import { authenticatedSession, firstAdminRow } from "./fixtures";
import { defaultHandlers, server } from "./server";

const config: ApiConfig = {
  apiUrl: "",
  token: "",
  csrfToken: "synthetic-csrf",
};

const setupCode = "synthetic-one-time-setup-code";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  server.use(...defaultHandlers);
});

describe("desktop pairing admin control", () => {
  it("uses the browser session and shows the setup code only in the open dialog", async () => {
    let requestBody: Record<string, unknown> | undefined;
    let csrfHeader = "";
    let requestCredentials = "";
    server.use(
      http.post("/api/v1/admin/desktop-pairings", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        csrfHeader = request.headers.get("X-TakSklad-CSRF") ?? "";
        requestCredentials = request.credentials;
        return HttpResponse.json({
          pairing_id: "synthetic-pairing-id",
          setup_code: setupCode,
          expires_at: "2030-01-01T00:00:00Z",
        }, { headers: { "Cache-Control": "no-store" } });
      }),
    );
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();

    render(<DesktopPairingControl config={config} onError={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Подключить складской ПК" }));
    await user.type(screen.getByLabelText(/Название ПК/), "  Основной склад  ");
    await user.click(screen.getByRole("button", { name: "Создать одноразовый код" }));

    expect(await screen.findByLabelText("Одноразовый код подключения")).toHaveTextContent(setupCode);
    expect(screen.getAllByText(setupCode)).toHaveLength(1);
    expect(requestBody).toEqual({ device_label: "Основной склад" });
    expect(csrfHeader).toBe("synthetic-csrf");
    expect(requestCredentials).toBe("same-origin");
    expect(storageWrite).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    expect(screen.queryByText(setupCode)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Подключить складской ПК" }));
    expect(screen.queryByText(setupCode)).not.toBeInTheDocument();
  });

  it("omits an empty optional label from the request", async () => {
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/admin/desktop-pairings", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          pairing_id: "synthetic-pairing-id",
          setup_code: setupCode,
          expires_at: "2030-01-01T00:00:00Z",
        });
      }),
    );
    const user = userEvent.setup();

    render(<DesktopPairingControl config={config} onError={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Подключить складской ПК" }));
    await user.click(screen.getByRole("button", { name: "Создать одноразовый код" }));

    await screen.findByLabelText("Одноразовый код подключения");
    expect(requestBody).toEqual({});
  });

  it("moves focus into the dialog and restores it after Escape", async () => {
    const user = userEvent.setup();
    render(<DesktopPairingControl config={config} onError={vi.fn()} />);
    const openButton = screen.getByRole("button", { name: "Подключить складской ПК" });

    await user.click(openButton);
    const labelInput = screen.getByLabelText(/Название ПК/);
    await waitFor(() => expect(labelInput).toHaveFocus());

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(openButton).toHaveFocus());
  });

  it("does not render the control without admin write permission", async () => {
    const readOnlySession: AuthSession = {
      ...authenticatedSession,
      role: "operator",
      permissions: authenticatedSession.permissions.filter((permission) => permission !== "admin:write"),
    };
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(readOnlySession)));

    render(<App />);

    await screen.findByRole("heading", { name: "Позиции заказов" });
    await screen.findByText(firstAdminRow.client);
    expect(screen.queryByRole("button", { name: "Подключить складской ПК" })).not.toBeInTheDocument();
  });

  it("keeps the pairing dialog free of automated accessibility violations", async () => {
    const user = userEvent.setup();
    const { container } = render(<DesktopPairingControl config={config} onError={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Подключить складской ПК" }));

    expect(await axe(container)).toHaveNoViolations();
  });
});
