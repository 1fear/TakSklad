import { KeyRound, Loader2, MonitorUp, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { createDesktopPairing, type DesktopPairingCreateResponse } from "../../api/desktopPairing";
import { ApiRequestError, type ApiConfig } from "../../api/core";

type DesktopPairingControlProps = {
  config: ApiConfig;
  disabled?: boolean;
  onError: (error: unknown) => void;
};

const MAX_DEVICE_LABEL_LENGTH = 80;

export default function DesktopPairingControl({
  config,
  disabled = false,
  onError,
}: DesktopPairingControlProps) {
  const [open, setOpen] = useState(false);
  const [deviceLabel, setDeviceLabel] = useState("");
  const [pairing, setPairing] = useState<DesktopPairingCreateResponse | null>(null);
  const [remainingSeconds, setRemainingSeconds] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const deviceLabelRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    deviceLabelRef.current?.focus();
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeDialog();
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  useEffect(() => {
    if (!pairing?.setup_code) return undefined;
    const expiresAt = Date.parse(pairing.expires_at);
    function refreshCountdown() {
      const seconds = Number.isFinite(expiresAt)
        ? Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000))
        : 0;
      setRemainingSeconds(seconds);
      if (seconds === 0) {
        setPairing((current) => current?.setup_code ? { ...current, setup_code: "" } : current);
      }
    }
    refreshCountdown();
    const timer = window.setInterval(refreshCountdown, 1_000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  function openDialog() {
    setError("");
    setPairing(null);
    setRemainingSeconds(0);
    setOpen(true);
  }

  function closeDialog() {
    setOpen(false);
    setPairing(null);
    setRemainingSeconds(0);
    setError("");
    setDeviceLabel("");
    openButtonRef.current?.focus();
  }

  async function createPairing() {
    setLoading(true);
    setError("");
    try {
      const created = await createDesktopPairing(config, deviceLabel);
      if (!created.setup_code || !created.pairing_id || !created.expires_at) {
        throw new Error("Сервер вернул неполные данные подключения.");
      }
      setPairing(created);
    } catch (failure) {
      const message = pairingErrorMessage(failure);
      setError(message);
      onError(failure);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <button
        ref={openButtonRef}
        className="ghost-button"
        onClick={openDialog}
        disabled={disabled}
        title="Подключить новый складской компьютер"
      >
        <MonitorUp size={18} />
        Подключить складской ПК
      </button>

      {open && (
        <div className="desktop-pairing-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.currentTarget === event.target) closeDialog();
        }}>
          <section
            className="desktop-pairing-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="desktop-pairing-title"
            aria-describedby="desktop-pairing-description"
          >
            <button className="desktop-pairing-close" onClick={closeDialog} aria-label="Закрыть подключение ПК">
              <X size={20} />
            </button>

            <div className="desktop-pairing-heading">
              <span className="desktop-pairing-icon" aria-hidden="true"><KeyRound size={24} /></span>
              <div>
                <h2 id="desktop-pairing-title">Подключить складской ПК</h2>
                <p id="desktop-pairing-description">
                  Сервер создаст одноразовый код. Права и идентификатор компьютера назначаются автоматически.
                </p>
              </div>
            </div>

            {!pairing && (
              <div className="desktop-pairing-form">
                <label htmlFor="desktop-device-label">Название ПК <span>(необязательно)</span></label>
                <input
                  ref={deviceLabelRef}
                  id="desktop-device-label"
                  type="text"
                  maxLength={MAX_DEVICE_LABEL_LENGTH}
                  value={deviceLabel}
                  onChange={(event) => setDeviceLabel(event.target.value)}
                  placeholder="Например, складской ПК"
                  disabled={loading}
                />
                <p className="desktop-pairing-warning">
                  Код нельзя пересылать и фотографировать. Введите его прямо на целевом складском ПК.
                </p>
                {error && <p className="desktop-pairing-error" role="alert">{error}</p>}
                <button className="primary-button desktop-pairing-submit" onClick={() => void createPairing()} disabled={loading}>
                  {loading ? <Loader2 className="spin" size={18} /> : <KeyRound size={18} />}
                  {loading ? "Создаём код..." : "Создать одноразовый код"}
                </button>
              </div>
            )}

            {pairing && (
              <div className="desktop-pairing-result">
                {pairing.setup_code ? (
                  <>
                    <p className="desktop-pairing-warning">
                      Код показывается только сейчас. Не пересылайте его и не делайте скриншот.
                    </p>
                    <code aria-label="Одноразовый код подключения">{pairing.setup_code}</code>
                    <p className="desktop-pairing-countdown" aria-live="polite">
                      Код действует ещё {formatRemainingTime(remainingSeconds)}
                    </p>
                  </>
                ) : (
                  <p className="desktop-pairing-error" role="alert">
                    Код истёк и удалён с экрана. Создайте новый код при необходимости.
                  </p>
                )}
                <button className="ghost-button" onClick={closeDialog}>Закрыть</button>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}

function pairingErrorMessage(error: unknown) {
  if (error instanceof ApiRequestError) {
    if (error.status === 401) return "Сессия закончилась. Войдите снова.";
    if (error.status === 403) return "Только администратор может подключать складские компьютеры.";
    if (error.status === 429) return "Слишком много запросов. Подождите и попробуйте снова.";
  }
  return error instanceof Error && error.message
    ? error.message
    : "Не удалось создать код подключения.";
}

function formatRemainingTime(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
