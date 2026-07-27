import { Loader2, PackageCheck } from "lucide-react";
import type { FormEvent, RefObject } from "react";

type ScannerInputProps = {
  inputRef: RefObject<HTMLInputElement | null>;
  value: string;
  disabled: boolean;
  submitDisabled: boolean;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};

export default function ScannerInput({
  inputRef,
  value,
  disabled,
  submitDisabled,
  busy,
  onChange,
  onSubmit,
}: ScannerInputProps) {
  return (
    <form className="warehouse-scanner-form" onSubmit={onSubmit}>
      <label className="warehouse-scanner-field">
        <span>КИЗ</span>
        <input
          ref={inputRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          autoComplete="off"
          autoCapitalize="none"
          inputMode="text"
          maxLength={512}
          placeholder="Отсканируйте код"
          disabled={disabled}
          aria-label="КИЗ"
          className="warehouse-scanner-input"
          spellCheck={false}
        />
      </label>
      <button className="primary-button warehouse-scanner-submit" type="submit" disabled={submitDisabled}>
        {busy ? <Loader2 className="spin" size={18} /> : <PackageCheck size={18} />}
        Записать
      </button>
    </form>
  );
}
