export interface RequestLease {
  readonly resource: string;
  readonly generation: number;
  readonly signal: AbortSignal;
  canCommit(): boolean;
  commit<T>(value: T, apply: (value: T) => void): boolean;
  abort(): void;
  finish(): void;
}

interface ActiveRequest {
  readonly generation: number;
  readonly controller: AbortController;
}

/** Owns at most one active request for each named resource. */
export class RequestCoordinator {
  private readonly active = new Map<string, ActiveRequest>();
  private nextGeneration = 1;

  begin(resource: string): RequestLease {
    this.abort(resource);

    const request: ActiveRequest = {
      generation: this.nextGeneration,
      controller: new AbortController(),
    };
    this.nextGeneration += 1;
    this.active.set(resource, request);

    const canCommit = () => (
      this.active.get(resource) === request
      && !request.controller.signal.aborted
    );

    return {
      resource,
      generation: request.generation,
      signal: request.controller.signal,
      canCommit,
      commit: <T>(value: T, apply: (committedValue: T) => void) => {
        if (!canCommit()) return false;
        apply(value);
        return true;
      },
      abort: () => {
        request.controller.abort();
        if (this.active.get(resource) === request) this.active.delete(resource);
      },
      finish: () => {
        if (this.active.get(resource) === request) this.active.delete(resource);
      },
    };
  }

  abort(resource: string): boolean {
    const request = this.active.get(resource);
    if (!request) return false;
    request.controller.abort();
    this.active.delete(resource);
    return true;
  }

  clear(): void {
    for (const request of this.active.values()) request.controller.abort();
    this.active.clear();
  }

  get activeCount(): number {
    return this.active.size;
  }

  isActive(resource: string): boolean {
    return this.active.has(resource);
  }
}
