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

  it("connects an assertive login error to both invalid credentials fields", async () => {
    server.use(
      http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)),
      http.post("/api/v1/auth/login", () => HttpResponse.json({ message: "Неверные данные" }, { status: 401 })),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Вход в панель" });

    const phone = screen.getByRole("textbox", { name: "Телефон" });
    const password = screen.getByLabelText("Пароль");
    await user.type(phone, "+998901234567");
    await user.type(password, "wrong-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("id", "login-error");
    expect(alert).toHaveAttribute("aria-live", "assertive");
    expect(phone).toHaveAttribute("aria-invalid", "true");
    expect(phone).toHaveAttribute("aria-describedby", "login-error");
    expect(password).toHaveAttribute("aria-invalid", "true");
    expect(password).toHaveAttribute("aria-describedby", "login-error");
  });

  it("has no automated axe violations on navigation and the orders table", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    expect(screen.getByRole("navigation", { name: "Разделы панели" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    const results = await axe(container);
    expect(results).toHaveNoViolations();

    await user.click(screen.getByRole("button", { name: "Склад" }));
    await screen.findByRole("heading", { name: "Склад · PostgreSQL" });
    expect(await axe(container)).toHaveNoViolations();
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
    expect(container.querySelector("tr[role='button']")).toBeNull();
    expect(screen.getByRole("button", { name: /Открыть инцидент/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Открыть событие/ })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});
