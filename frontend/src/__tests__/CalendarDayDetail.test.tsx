import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CalendarDayDetail } from "../features/logistics/CalendarDayDetail";
import { logisticsCalendarDayOrders } from "./fixtures";

const day = {
  date: "2026-08-07",
  weekday: 4,
  is_weekend: false,
  is_non_working: false,
  is_manual: false,
  reason: "",
  source: "default",
  orders_count: 139,
  active_orders: 27,
  completed_orders: 112,
  returned_orders: 10,
  planned_blocks: 809,
  city_orders: 96,
  region_orders: 43,
  city_returns: 7,
  region_returns: 3,
  city_blocks: 612,
  region_blocks: 197,
  excluded_orders: 5,
  clients: ["Тест Клиент 1"],
};

const noop = () => {};

describe("CalendarDayDetail", () => {
  it("показывает разбивку город и область", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders as never}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("group", { name: /Город/ })).toHaveTextContent("96");
    expect(screen.getByRole("group", { name: /Область/ })).toHaveTextContent("43");
    expect(screen.getByText(/вне логистики/i)).toHaveTextContent("5");
  });

  it("предупреждает о пустом справочнике областных точек", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty
        canAdminWrite={false}
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/справочник областных точек пуст/i);
  });

  it("показывает элементы управления только при праве на запись", () => {
    const { unmount } = render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Причина / комментарий" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Не работает" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Работает" })).toBeInTheDocument();

    unmount();

    render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.queryByRole("textbox", { name: "Причина / комментарий" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Не работает" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Работает" })).not.toBeInTheDocument();
  });
});
