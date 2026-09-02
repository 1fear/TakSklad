import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { LogisticsCalendarDayOrders } from "../api";
import { CalendarDayDetail } from "../features/logistics/CalendarDayDetail";
import { logisticsCalendarDayOrders, logisticsCalendarDayOrdersWithManualStop } from "./fixtures";

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

// Пропсы ручных точек одинаковы во всех рендерах этого файла: сама по себе
// ручная точка проверяется отдельным блоком ниже
const manualStopProps = {
  manualStopSearchResults: [],
  manualStopSearching: false,
  onManualStopSearch: noop,
  onManualStopSave: noop,
  onManualStopDelete: noop,
};

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
        {...manualStopProps}
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
        {...manualStopProps}
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
        {...manualStopProps}
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
        {...manualStopProps}
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
        {...manualStopProps}
      />,
    );

    expect(screen.getByRole("row", { name: /Тест Клиент 1/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    expect(screen.getByRole("row", { name: /Тест Клиент 2/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Заказы" }));
    expect(screen.queryByRole("row", { name: /Тест Клиент 2/ })).not.toBeInTheDocument();
  });

  it("показывает подписи статуса жизненного цикла и меняет их вместе с данными", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
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
        {...manualStopProps}
      />,
    );

    const assemblingRow = screen.getByRole("row", { name: /Тест Клиент 1/ });
    expect(assemblingRow).toHaveTextContent("В сборке");
    expect(within(assemblingRow).getByText("В сборке")).toHaveClass("status-badge", "assembling");

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    const returnedRow = screen.getByRole("row", { name: /Тест Клиент 2/ });
    expect(returnedRow).toHaveTextContent("Возврат");
    expect(within(returnedRow).getByText("Возврат")).toHaveClass("status-badge", "ret");
    expect(returnedRow).toHaveClass("ret-row");

    await user.click(screen.getByRole("tab", { name: /Город/ }));

    const assembledOrders: LogisticsCalendarDayOrders = {
      ...logisticsCalendarDayOrders,
      orders: logisticsCalendarDayOrders.orders.map((row) =>
        row.order_id === "order-1" ? { ...row, lifecycle_status: "delivered" as const } : row,
      ),
    };

    rerender(
      <CalendarDayDetail
        day={day}
        dayOrders={assembledOrders}
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
        {...manualStopProps}
      />,
    );

    const deliveredRow = screen.getByRole("row", { name: /Тест Клиент 1/ });
    expect(deliveredRow).toHaveTextContent("Доставлен");
    expect(within(deliveredRow).getByText("Доставлен")).toHaveClass("status-badge", "delivered");
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
        {...manualStopProps}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX город/ }));
    expect(onDownload).toHaveBeenCalledWith("city");

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX область/ }));
    expect(onDownload).toHaveBeenCalledWith("region");
  });

  it("показывает подсказку про возвраты рядом с кнопкой выгрузки на обеих вкладках", async () => {
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
        {...manualStopProps}
      />,
    );

    expect(screen.getByText("Возвраты в XLSX не входят")).toBeVisible();

    await user.click(screen.getByRole("tab", { name: /Область/ }));
    expect(screen.getByText("Возвраты в XLSX не входят")).toBeVisible();
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
        {...manualStopProps}
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
        {...manualStopProps}
      />,
    );

    expect(screen.getByRole("button", { name: "Не работает" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Работает" })).toBeDisabled();
  });

  describe("ручные точки", () => {
    it("показывает ручную точку отдельной строкой и не примешивает её к счётчикам дня", () => {
      render(
        <CalendarDayDetail
          day={day}
          dayOrders={logisticsCalendarDayOrdersWithManualStop}
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
          {...manualStopProps}
        />,
      );

      expect(screen.getByText("Тест Филиал")).toBeInTheDocument();
      expect(screen.getByText("Ручная точка")).toBeInTheDocument();
      // цифры дня остаются про заказы, ручные точки идут отдельной строкой
      expect(screen.getByRole("group", { name: /Город/ })).toHaveTextContent("612");
      expect(screen.getByRole("group", { name: /Итого/ })).toHaveTextContent(
        "+ 1 ручная точка, 12 блоков, в счёт заказов и блоков они не входят",
      );
    });

    it("фильтр «Ручные» прячет заказы и оставляет только ручные точки", async () => {
      const user = userEvent.setup();
      render(
        <CalendarDayDetail
          day={day}
          dayOrders={logisticsCalendarDayOrdersWithManualStop}
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
          {...manualStopProps}
        />,
      );

      await user.click(screen.getByRole("button", { name: /Ручные/ }));

      expect(screen.getByText("Тест Филиал")).toBeInTheDocument();
      expect(screen.queryByText("Тест Клиент 1")).not.toBeInTheDocument();
    });

    it("не даёт сохранить точку без координат и отдаёт наверх готовую полезную нагрузку", async () => {
      const user = userEvent.setup();
      const onManualStopSave = vi.fn();
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
          {...manualStopProps}
          onManualStopSave={onManualStopSave}
        />,
      );

      await user.click(screen.getByRole("button", { name: /Добавить точку/ }));
      await user.type(screen.getByLabelText("Клиент"), "Тест Ручная Точка");
      await user.type(screen.getByLabelText("Адрес"), "Ташкент, ручной адрес 1");
      await user.click(screen.getByRole("button", { name: "Сохранить точку" }));

      expect(onManualStopSave).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent("Координаты вводятся парой чисел");

      await user.type(screen.getByLabelText("Координаты"), "41.311081, 69.240562");
      await user.click(screen.getByRole("button", { name: "Сохранить точку" }));

      expect(onManualStopSave).toHaveBeenCalledWith(expect.objectContaining({
        service_date: "2026-08-07",
        client_name: "Тест Ручная Точка",
        address: "Ташкент, ручной адрес 1",
        coordinates: "41.311081, 69.240562",
        blocks: 0,
        save_to_directory: true,
      }));
    });

    it("не показывает форму и кнопки правки без права записи", () => {
      render(
        <CalendarDayDetail
          day={day}
          dayOrders={logisticsCalendarDayOrdersWithManualStop}
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
          {...manualStopProps}
        />,
      );

      expect(screen.getByText("Тест Филиал")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Добавить точку/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Правка" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Убрать" })).not.toBeInTheDocument();
    });

    it("кнопка «Убрать» отдаёт наверх id точки", async () => {
      const user = userEvent.setup();
      const onManualStopDelete = vi.fn();
      render(
        <CalendarDayDetail
          day={day}
          dayOrders={logisticsCalendarDayOrdersWithManualStop}
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
          {...manualStopProps}
          onManualStopDelete={onManualStopDelete}
        />,
      );

      await user.click(screen.getByRole("button", { name: "Убрать" }));

      expect(onManualStopDelete).toHaveBeenCalledWith("manual-1");
    });
  });
});
