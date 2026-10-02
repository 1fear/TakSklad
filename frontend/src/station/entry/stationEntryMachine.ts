/**
 * What "/" does when it opens: the decision table, with no React in it.
 *
 *   session of the station          -> ready
 *   session of anyone else          -> redirect-admin (never sign in over an admin cookie)
 *   no session, station login ok    -> ready
 *   no session, 403 network denied  -> redirect-admin (not the warehouse network)
 *   no session, network error / 5xx / 429 -> offline, retry in not less than 30 s
 *   ready, but another window holds the station lock -> duplicate-tab
 */

import type { AuthSession } from "../../api/auth";
import { ApiRequestError } from "../../api/core";

export const STATION_NETWORK_DENIED_CODE = "station_network_denied";

/** The station signs in again no more often than this (spec section 2). */
export const STATION_RETRY_MIN_MS = 30_000;

export type StationEntryState =
  | { kind: "checking" }
  | { kind: "ready"; session: AuthSession; csrfToken: string }
  | { kind: "redirect-admin" }
  | { kind: "offline"; retryInMs: number }
  | { kind: "duplicate-tab" };

export type StationEntryDeps = {
  getSession(): Promise<AuthSession>;
  login(): Promise<AuthSession>;
  /** True when this window now owns the station, false when another window does. */
  acquireLock(): Promise<boolean>;
};

function isStationSession(session: AuthSession): boolean {
  return session.authenticated && session.role === "station";
}

function ready(session: AuthSession): StationEntryState {
  return { kind: "ready", session, csrfToken: session.csrf_token || "" };
}

/** 30 s for any failure, the server's Retry-After when it asks for longer (429). */
export function retryDelayMs(error: unknown): number {
  const asked = error instanceof ApiRequestError && error.status === 429 ? error.retryAfterSeconds * 1000 : 0;
  return Math.max(STATION_RETRY_MIN_MS, asked);
}

function offline(error: unknown): StationEntryState {
  return { kind: "offline", retryInMs: retryDelayMs(error) };
}

function afterLoginFailure(error: unknown): StationEntryState {
  if (error instanceof ApiRequestError && error.status === 403 && error.code === STATION_NETWORK_DENIED_CODE) {
    return { kind: "redirect-admin" };
  }
  return offline(error);
}

/** One pass of the table. Never throws: every failure becomes a state. */
export async function resolveStationEntry(deps: StationEntryDeps): Promise<StationEntryState> {
  let session: AuthSession;
  try {
    session = await deps.getSession();
  } catch (error) {
    return offline(error);
  }
  if (session.authenticated) {
    return isStationSession(session) ? ready(session) : { kind: "redirect-admin" };
  }

  try {
    const signedIn = await deps.login();
    // A 200 that is not a station session is not something to build a screen on.
    return isStationSession(signedIn) ? ready(signedIn) : offline(null);
  } catch (error) {
    return afterLoginFailure(error);
  }
}

/**
 * Runs the table until it settles, publishing every state it passes through.
 * `offline` waits out `retryInMs` and tries again, like the desktop started without a network.
 */
export async function runStationEntry(
  deps: StationEntryDeps,
  publish: (state: StationEntryState) => void,
  signal: AbortSignal,
  sleep: (ms: number, signal: AbortSignal) => Promise<void> = abortableSleep,
): Promise<void> {
  publish({ kind: "checking" });
  while (!signal.aborted) {
    const state = await resolveStationEntry(deps);
    if (signal.aborted) return;

    if (state.kind === "ready" && !(await deps.acquireLock())) {
      publish({ kind: "duplicate-tab" });
      return;
    }
    publish(state);
    if (state.kind !== "offline") return;
    await sleep(state.retryInMs, signal);
  }
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}
