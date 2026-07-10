import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { anonymousSession } from "./fixtures";
import { defaultHandlers, server } from "./server";

beforeEach(() => server.use(...defaultHandlers));

describe("focused accessibility characterization", () => {
  it("has no automated axe violations on the login semantic surface", async () => {
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)));
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "Вход в панель" });

    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });

  it("has no automated axe violations on navigation and the orders table", async () => {
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    expect(screen.getByRole("navigation", { name: "Разделы панели" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });

  it("has no automated axe violations on client points and incidents", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    await user.click(screen.getByRole("button", { name: "Клиенты" }));
    await screen.findByRole("heading", { name: "Клиенты и таймслоты" });
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole("button", { name: "История действий" }));
    await user.click(screen.getByRole("button", { name: "Инциденты" }));
    await screen.findByRole("heading", { name: "Инциденты и очередь" });
    expect(await axe(container)).toHaveNoViolations();
  });
});
