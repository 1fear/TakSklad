import logging
import os
import subprocess
import threading
import time

import tkinter as tk
from tkinter import messagebox

from .config import (
    APP_VERSION,
    BG_MAIN,
    ERROR_BG,
    ERROR_FG,
    FG_MUTED,
    UPDATE_INFO_URL,
    UPDATE_LOG_FILE,
    UPDATE_RETRY_COOLDOWN_SECONDS,
)
from .storage import load_data_section, save_data_section
from .update_service import compare_versions, fetch_update_info, package_transition_required, prepare_update_installer
from .utils import normalize_text, parse_int_value


def manifest_bool(value):
    if isinstance(value, bool):
        return value
    return normalize_text(value).lower() in {"1", "true", "yes", "on", "да"}


def format_update_recovery_message(reason=""):
    lines = []
    reason_text = normalize_text(reason)
    if reason_text:
        lines.append(reason_text)
        lines.append("")
    lines.append(f"Лог обновления: {UPDATE_LOG_FILE}")
    lines.append(
        "Безопасное действие: закройте TakSklad, установите свежий Windows-архив "
        "и запускайте только новый TakSklad.exe. Старую версию для сканирования не используйте."
    )
    return "\n".join(lines)


class UpdateMixin:
    def auto_update_supported(self):
        return os.name == "nt"

    def ensure_update_allowed(self):
        if not self.update_required:
            return True
        self.show_error("Требуется обновить приложение перед работой")
        return False

    def apply_required_update_lock(self):
        self.status_var.set("⛔ Требуется обновление приложения")
        self.safe_config(self.status_label, bg=ERROR_BG, fg=ERROR_FG)
        for button_name in (
            "refresh_btn",
            "import_btn",
            "catalog_btn",
            "control_btn",
            "select_btn",
            "undo_btn",
            "next_product_btn",
            "finish_btn",
            "report_btn",
        ):
            button = getattr(self, button_name, None)
            if button:
                self.safe_config(button, state="disabled")

    def show_update_recovery(self, reason):
        message = format_update_recovery_message(reason)
        show_error = getattr(self, "show_error", None)
        if callable(show_error):
            show_error(message, popup=False)
        else:
            self.status_var.set(f"⛔ {message}")

    def start_auto_update(self, update_info):
        self.status_var.set("⏳ Скачиваю обновление...")
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)

        def worker():
            try:
                updater_path = prepare_update_installer(update_info)
            except Exception as exc:
                logging.exception("Не удалось подготовить автообновление")
                try:
                    self.after(
                        0,
                        lambda exc=exc: self.show_critical_error(
                            "Не удалось обновить приложение автоматически",
                            format_update_recovery_message(str(exc)),
                        ),
                    )
                except tk.TclError:
                    pass
                return

            try:
                self.after(0, lambda: self.run_update_installer(updater_path))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def run_update_installer(self, updater_path):
        self.status_var.set("⏳ Устанавливаю обновление...")
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        if updater_path.lower().endswith(".ps1"):
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", updater_path],
                creationflags=creationflags,
            )
        else:
            subprocess.Popen(["cmd", "/c", updater_path], creationflags=creationflags)
        self.destroy()

    def check_for_updates(self):
        if not UPDATE_INFO_URL:
            return

        def worker():
            try:
                update_info = fetch_update_info()
            except Exception as exc:
                logging.info("Не удалось проверить обновления: %s", exc)
                return

            try:
                self.after(0, lambda: self.handle_update_info(update_info))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def handle_update_info(self, update_info):
        if not update_info:
            return

        latest_version = normalize_text(update_info.get("latest_version"))
        min_supported_version = normalize_text(update_info.get("min_supported_version"))
        message = normalize_text(update_info.get("message"))
        update_available = bool(latest_version) and compare_versions(APP_VERSION, latest_version) < 0
        below_min_version = bool(min_supported_version) and compare_versions(APP_VERSION, min_supported_version) < 0
        package_update_required = package_transition_required(update_info)
        forced_update = below_min_version or manifest_bool(update_info.get("mandatory"))

        if not update_available and not below_min_version and not package_update_required:
            return

        if not self.auto_update_supported():
            self.update_info = update_info
            self.update_required = False
            target_version = latest_version or min_supported_version or "новой версии"
            self.status_var.set(
                f"ℹ Доступно обновление {target_version}. На Mac установите свежий архив вручную."
            )
            logging.info(
                "Автообновление пропущено: платформа не поддерживается (current=%s latest=%s)",
                APP_VERSION,
                latest_version,
            )
            return

        # Cooldown на случай зацикленного автообновления. Если пользователь
        # отказался или предыдущая попытка упала, не дёргаем апдейтер сразу же
        # на каждом старте. UPDATE_RETRY_COOLDOWN_SECONDS = 3600.
        skip_state = load_data_section("update_skip_state", {})
        if not isinstance(skip_state, dict):
            skip_state = {}
        last_attempt_ts = parse_int_value(skip_state.get("last_attempt_ts"))
        last_attempt_version = normalize_text(skip_state.get("last_attempt_version"))
        now_ts = int(time.time())
        if (
            last_attempt_ts > 0
            and now_ts - last_attempt_ts < UPDATE_RETRY_COOLDOWN_SECONDS
            and last_attempt_version == latest_version
        ):
            remaining = UPDATE_RETRY_COOLDOWN_SECONDS - (now_ts - last_attempt_ts)
            logging.info(
                "Автообновление пропущено по cooldown (%d сек осталось, версия %s)",
                remaining,
                latest_version,
            )
            self.update_info = update_info
            if forced_update:
                self.update_required = True
                self.apply_required_update_lock()
                self.show_update_recovery(
                    f"Обязательное обновление {latest_version} уже пытались установить. "
                    f"Повторная автоустановка будет доступна через {remaining} сек."
                )
            else:
                self.update_required = False
                self.status_var.set(
                    f"ℹ Доступно обновление {latest_version}. Откладываю до перезапуска."
                )
            return

        # Спрашиваем пользователя перед установкой — без этого автообновление
        # уходит в цикл, если установка падает: updater запускает старый exe,
        # он снова видит «нужно обновиться» и снова запускает updater.
        prompt_lines = [f"Доступно обновление до версии {latest_version}."]
        if package_update_required:
            prompt_lines.append("Требуется переход на новый формат сборки (onedir).")
        if below_min_version:
            prompt_lines.append("Текущая версия больше не поддерживается.")
        if forced_update and not below_min_version:
            prompt_lines.append("Это обязательное обновление.")
        if message:
            prompt_lines.append("")
            prompt_lines.append(message)
        prompt_lines.append("")
        prompt_lines.append("Установить сейчас? Приложение перезапустится.")

        user_confirmed = messagebox.askyesno(
            "Обновление TakSklad",
            "\n".join(prompt_lines),
        )

        # Сохраняем попытку (даже если пользователь отказался) — это и есть
        # cooldown: следующая проверка той же версии не сработает ещё час.
        skip_state["last_attempt_ts"] = now_ts
        skip_state["last_attempt_version"] = latest_version
        skip_state["last_user_action"] = "accepted" if user_confirmed else "declined"
        save_data_section("update_skip_state", skip_state)

        if not user_confirmed:
            self.update_info = update_info
            if forced_update:
                self.update_required = True
                self.apply_required_update_lock()
                self.show_update_recovery(
                    f"Обязательное обновление {latest_version} отклонено. Работа на этой версии заблокирована."
                )
            else:
                self.update_required = False
                self.status_var.set(
                    f"ℹ Обновление {latest_version} отложено. Будет предложено снова через час."
                )
            logging.info("Автообновление отклонено пользователем (версия %s)", latest_version)
            return

        self.update_info = update_info
        self.update_required = True
        self.apply_required_update_lock()

        self.status_var.set("⏳ Найдено обновление, начинаю установку...")
        logging.info(
            "Запущено автообновление: current=%s latest=%s below_min=%s package_transition=%s message=%s",
            APP_VERSION,
            latest_version,
            below_min_version,
            package_update_required,
            message,
        )
        self.start_auto_update(update_info)
