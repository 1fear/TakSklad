"""Доставка общего файла КИЗ за день следом за SkladBot daily-отчетом."""

from __future__ import annotations

from .telegram_output_contract import kiz_daily_report_caption


def resolve_kiz_date_report_builder(sender):
    builder = getattr(sender, "kiz_date_report_builder", None)
    if builder:
        return builder
    from .kiz_reports_service import build_kiz_date_report_xlsx

    return build_kiz_date_report_xlsx


def send_daily_kiz_export(sender, chat_id, report_date, scheduled=False, emit_progress=None):
    """Строит файл всех КИЗ за дату отчета и отправляет его тем же адресатом.

    Daily-отчет к этому моменту уже доставлен, поэтому отсутствие КИЗ за день
    или сбой сборки не считаются провалом доставки: они только фиксируются в
    progress, а сам daily остается отправленным.
    """
    builder = resolve_kiz_date_report_builder(sender)
    try:
        with sender._scheduled_session_factory()() as db:
            content, filename = builder(db, report_date.isoformat())
    except Exception as exc:
        if emit_progress:
            emit_progress(
                "kiz export skipped",
                report_date=report_date.isoformat(),
                reason=type(exc).__name__,
            )
        return None
    caption = kiz_daily_report_caption(report_date)
    if scheduled:
        return sender.send_document(chat_id, content, filename, caption=caption)
    return sender.safe_send_document(chat_id, content, filename, caption=caption)
