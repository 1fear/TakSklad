export const SEARCH_DEBOUNCE_MS = 275;

export interface DebouncedTask<Arguments extends unknown[]> {
  schedule(...args: Arguments): void;
  cancel(): void;
  flush(): void;
  readonly pending: boolean;
}

export function createDebouncedTask<Arguments extends unknown[]>(
  callback: (...args: Arguments) => void,
  delayMs = SEARCH_DEBOUNCE_MS,
): DebouncedTask<Arguments> {
  if (delayMs < 250 || delayMs > 300) {
    throw new RangeError("Search debounce must stay between 250 and 300 ms.");
  }

  let timeout: ReturnType<typeof setTimeout> | undefined;
  let pendingArgs: Arguments | undefined;

  const invoke = () => {
    if (!pendingArgs) return;
    const args = pendingArgs;
    pendingArgs = undefined;
    timeout = undefined;
    callback(...args);
  };

  return {
    schedule: (...args) => {
      pendingArgs = args;
      if (timeout !== undefined) clearTimeout(timeout);
      timeout = setTimeout(invoke, delayMs);
    },
    cancel: () => {
      pendingArgs = undefined;
      if (timeout !== undefined) clearTimeout(timeout);
      timeout = undefined;
    },
    flush: () => {
      if (timeout !== undefined) clearTimeout(timeout);
      invoke();
    },
    get pending() {
      return pendingArgs !== undefined;
    },
  };
}
