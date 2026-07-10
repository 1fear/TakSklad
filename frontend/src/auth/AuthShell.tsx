import { AlertCircle, Database, History, KeyRound, Loader2, Lock, PackageCheck, Phone, Server, ShieldCheck } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useRef } from "react";

export function LoadingGate() {
  return (
    <div className="login-shell loading-gate" role="status" aria-live="polite">
      <div className="loading-mark">
        <Loader2 className="spin" size={24} />
        <span>Загружаем доступ...</span>
      </div>
    </div>
  );
}

export type LoginScreenProps = {
  phone: string;
  password: string;
  error: string;
  loading: boolean;
  onPhoneChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
};

export function LoginScreen({
  phone,
  password,
  error,
  loading,
  onPhoneChange,
  onPasswordChange,
  onSubmit,
}: LoginScreenProps) {
  const phoneRef = useRef<HTMLInputElement>(null);
  useEffect(() => phoneRef.current?.focus({ preventScroll: true }), []);

  return (
    <main className="login-shell">
      <section className="login-brand">
        <div className="login-logo-row">
          <img src="/taksklad.png" alt="" />
          <div>
            <strong>TakSklad</strong>
            <span>Складская web-панель</span>
          </div>
        </div>
        <div className="login-copy">
          <p>Операционный контур склада</p>
          <h1>Доступ к заказам, синхронизации и журналу действий</h1>
        </div>
        <div className="login-status-grid">
          <span><Database size={16} /> Google Sheets</span>
          <span><Server size={16} /> VDS backend</span>
          <span><PackageCheck size={16} /> SkladBot</span>
          <span><History size={16} /> Audit log</span>
        </div>
      </section>

      <section className="login-panel" aria-label="Вход в панель">
        <div className="login-panel-head">
          <Lock size={22} />
          <div>
            <h2>Вход в панель</h2>
            <span>Используйте рабочий телефон и пароль.</span>
          </div>
        </div>
        <form className="login-form" onSubmit={onSubmit}>
          <label>
            <span>Телефон</span>
            <div className="login-input">
              <Phone size={18} />
              <input
                id="login-phone"
                ref={phoneRef}
                inputMode="tel"
                autoComplete="username"
                value={phone}
                onChange={(event) => onPhoneChange(event.target.value)}
                placeholder="+998 XX XXX XX XX"
                aria-invalid={Boolean(error)}
                aria-describedby={error ? "login-error" : undefined}
              />
            </div>
          </label>
          <label>
            <span>Пароль</span>
            <div className="login-input">
              <KeyRound size={18} />
              <input
                id="login-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => onPasswordChange(event.target.value)}
                placeholder="Введите пароль"
                aria-invalid={Boolean(error)}
                aria-describedby={error ? "login-error" : undefined}
              />
            </div>
          </label>
          {error && (
            <div id="login-error" className="login-error" role="alert" aria-live="assertive">
              <AlertCircle size={17} />
              {error}
            </div>
          )}
          <button className="login-submit" type="submit" disabled={loading || !phone.trim() || !password}>
            {loading ? <Loader2 className="spin" size={18} /> : <ShieldCheck size={18} />}
            {loading ? "Проверяем доступ..." : "Войти"}
          </button>
        </form>
        <p className="login-footnote">Нет доступа? Обратитесь к администратору склада.</p>
      </section>
    </main>
  );
}
