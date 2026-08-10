import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

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
        dayOrders={logisticsCalendarDayOrders}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction=""
        canGoPrevDay
        canGoNextDay
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
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(/справочник областных точек пуст/i);
  });

  it("показывает кнопки статуса только при праве на запись, причину показывает всегда", () => {
    const { unmount } = render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction=""
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Причина / комментарий" })).toBeEnabled();
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
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Причина / комментарий" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Не работает" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Работает" })).not.toBeInTheDocument();
  });

  it("переключает вкладку на область и фильтрует возвраты", async () => {
    const user = userEvent.setup();
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("row", { name: /Тест Клиент 1/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    expect(screen.getByRole("row", { name: /Тест Клиент 2/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Заказы" }));
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();
  });

  it("выгружает XLSX активной вкладки", async () => {
    const user = userEvent.setup();
    const onDownload = vi.fn();
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={logisticsCalendarDayOrders}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={onDownload}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX город/ }));
    expect(onDownload).toHaveBeenCalledWith("city");

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX область/ }));
    expect(onDownload).toHaveBeenCalledWith("region");
  });

  it("блокирует стрелки дня на границе доступных дней", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite={false}
        busyAction=""
        canGoPrevDay={false}
        canGoNextDay={false}
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("button", { name: "Предыдущий день" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Следующий день" })).toBeDisabled();
  });

  it("блокирует кнопки статуса дня при любом идущем действии, не только своём", () => {
    render(
      <CalendarDayDetail
        day={day}
        dayOrders={null}
        loading={false}
        regionDirectoryEmpty={false}
        canAdminWrite
        busyAction="calendar-report:2026-08-07:city"
        canGoPrevDay
        canGoNextDay
        onPrevDay={noop}
        onNextDay={noop}
        onSaveDay={noop}
        onDownload={noop}
      />,
    );

    expect(screen.getByRole("button", { name: "Не работает" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Работает" })).toBeDisabled();
  });
});
