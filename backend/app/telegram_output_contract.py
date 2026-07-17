"""Dependency-light builders for approved Telegram-facing outputs."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from typing import Any


REQUEST_CATEGORY_SHIPMENT = "Отгрузка"
REQUEST_CATEGORY_DEFECT_SHIPMENT = "Отгрузка в браке"
REQUEST_CATEGORY_RETURN = "Возврат"
REQUEST_CATEGORY_RECEIVING = "Приемка"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _display_date(value: Any) -> str:
    parsed = _date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else _text(value)


def _iso_date(value: Any) -> str:
    parsed = _date(value)
    return parsed.isoformat() if parsed else _text(value)


def smartup_export_caption(
    export_date: date,
    slot_label: str,
    part: int,
    selected_orders: int,
    rows: int,
    delivery_dates: list[str],
) -> str:
    delivery_display = ", ".join(_display_date(value) for value in delivery_dates if value) or "-"
    return (
        f"Smartup выгрузка за {_display_date(export_date)}, слот {_text(slot_label) or '-'}, "
        f"часть {part}. Терминал. Заказов: {selected_orders}, строк: {rows}. "
        f"Даты отгрузки: {delivery_display}."
    )


def smartup_export_filename(export_date: date, part: int) -> str:
    return f"Терминал {_display_date(export_date)} Часть {part}.xlsx"


def logistics_report_caption(report_date: Any) -> str:
    return f"Отчёт логистики за {_display_date(report_date)}"


def logistics_report_filename(report_date: Any) -> str:
    return f"TakSklad_логистика_{_display_date(report_date)}.xlsx"


def build_skladbot_daily_report_message(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    category_counts = summary.get("category_counts") or {}
    blocks = summary.get("request_blocks_by_category") or {}
    lines = [
        f"SkladBot daily за {_iso_date(report.get('report_date'))}",
        f"Отгрузка: {category_counts.get(REQUEST_CATEGORY_SHIPMENT, 0)} заявок, {blocks.get(REQUEST_CATEGORY_SHIPMENT, 0)} блоков",
        f"Отгрузка в браке: {category_counts.get(REQUEST_CATEGORY_DEFECT_SHIPMENT, 0)} заявок, {blocks.get(REQUEST_CATEGORY_DEFECT_SHIPMENT, 0)} блоков",
        f"Возврат: {category_counts.get(REQUEST_CATEGORY_RETURN, 0)} заявок, {blocks.get(REQUEST_CATEGORY_RETURN, 0)} блоков",
        f"Приемка: {category_counts.get(REQUEST_CATEGORY_RECEIVING, 0)} заявок, {blocks.get(REQUEST_CATEGORY_RECEIVING, 0)} блоков",
        f"Актуальный остаток: {summary.get('stock_total', 0)}",
    ]
    return "\n".join(lines)


def daily_report_filename(report_date: Any) -> str:
    parsed = _date(report_date)
    date_text = parsed.strftime("%d.%m.%Y") if parsed else "unknown"
    return f"TakSklad_SkladBot_daily_{date_text}.xlsx"


def daily_report_caption(report_date: Any) -> str:
    return f"SkladBot отчет за {_display_date(report_date)}"


def blocked_admin_notification_text(reason: Any) -> str:
    return "\n".join([
        "TakSklad: служебное Telegram-уведомление заблокировано",
        f"Причина: {_text(reason)}",
        "Исходный payload не отправлен.",
    ])


def runtime_output_artifacts() -> dict[str, dict[str, str]]:
    sample_date = date(2030, 1, 2)
    daily_report = {
        "report_date": sample_date,
        "summary": {
            "category_counts": {
                REQUEST_CATEGORY_SHIPMENT: 1,
                REQUEST_CATEGORY_DEFECT_SHIPMENT: 2,
                REQUEST_CATEGORY_RETURN: 3,
                REQUEST_CATEGORY_RECEIVING: 4,
            },
            "request_blocks_by_category": {
                REQUEST_CATEGORY_SHIPMENT: 5,
                REQUEST_CATEGORY_DEFECT_SHIPMENT: 6,
                REQUEST_CATEGORY_RETURN: 7,
                REQUEST_CATEGORY_RECEIVING: 8,
            },
            "movement_in_rows": 9,
            "movement_in_amount": 10,
            "movement_out_rows": 11,
            "movement_out_amount": 12,
            "stock_total": 13,
        },
    }
    return {
        "smartup_client_export": {
            "caption": smartup_export_caption(
                sample_date, "17:50", 1, 2, 3, [sample_date.isoformat()]
            ),
            "filename": smartup_export_filename(sample_date, 1),
        },
        "smartup_logistics_report": {
            "caption": logistics_report_caption(sample_date),
            "filename": logistics_report_filename(sample_date),
        },
        "skladbot_daily_report": {
            "message": build_skladbot_daily_report_message(daily_report),
            "caption": daily_report_caption(sample_date),
            "filename": daily_report_filename(sample_date),
        },
        "admin_error": {
            "message": blocked_admin_notification_text("unknown_kind"),
        },
    }


def runtime_output_policy_hashes() -> dict[str, str]:
    return {
        kind: hashlib.sha256(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for kind, artifact in runtime_output_artifacts().items()
    }
