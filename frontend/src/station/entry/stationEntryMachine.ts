/**
 * What "/" does when it opens: the decision table, with no React in it.
 *
 *   session of the station          -> ready
 *   session of anyone else          -> redirect-admin (never sign in over an admin cookie)
 *   no session, another window holds the station lock -> duplicate-tab, and login is never called
 *   no session, station login ok    -> ready
 *   no session, 403 network denied  -> redirect-admin (not the warehouse network)
 *   no session, network error / 5xx / 429 -> offline, retry in not less than 30 s
 *   ready, but another window holds the station lock -> duplicate-tab
 *
 * Login replaces the session cookie of the whole browser (the CSRF token derives from it), so a window that does not
 * own the station must never sign in: with no session the lock is taken before login, with a session of the station
 * right after the decision.
 */

import type { AuthSession } from "../../api/auth";
import { ApiRequestError } from "../../api/core";

export const STATION_NETWORK_DENIED_CODE = "station_network_denied";

/** The station signs in again no more often than this (spec section 2). */
export const STATION_RETRY_MIN_MS = 30_000;

/** ...and waits no longer than this, whatever Retry-After says (a huge value would overflow the timer). */
export const STATION_RETRY_MAX_MS = 600_000;

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

/** 30 s for any failure, the server's Retry-After when it asks for longer (429), but never more than ten minutes. */
export function retryDelayMs(error: unknown): number {
  const asked = error instanceof ApiRequestError && error.status === 429 ? error.retryAfterSeconds * 1000 : 0;
  return Math.min(STATION_RETRY_MAX_MS, Math.max(STATION_RETRY_MIN_MS, asked));
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

  // Nothing is sent from a window that does not own the station: login would swap the owner's cookie.
  if (!(await deps.acquireLock())) return { kind: "duplicate-tab" };

  try {
    const signedIn = await deps.login();
    // A 200 that is not a station session is not something to build a screen on.
    return isStationSession(signedIn) ? ready(signedIn) : offline(null);
  } catch (error) {
    return afterLoginFailure(error);
  }
}

/** Why `refreshStationSession` gave up. Nothing was signed in and no cookie was replaced. */
export class StationSessionRefusedError extends Error {
  /** `other-role`: the browser holds a session of someone else. `not-station`: the station login answered with another role. */
  reason: "other-role" | "not-station";

  constructor(reason: "other-role" | "not-station") {
    super(reason === "other-role" ? "The browser holds a session that is not the station's" : "Station login did not return a station session");
    this.name = "StationSessionRefusedError";
    this.reason = reason;
  }
}

/**
 * The contract of the queue's `relogin` hook (spec section 4): read `/auth/session` first, so a session of the station
 * hands back its fresh csrf token and a session of another role is never replaced; sign in only when there is none.
 */
export async function refreshStationSession(deps: Pick<StationEntryDeps, "getSession" | "login">): Promise<AuthSession> {
  const current = await deps.getSession();
  if (current.authenticated) {
    if (isStationSession(current)) return current;
    throw new StationSessionRefusedError("other-role");
  }
  const signedIn = await deps.login();
  if (!isStationSession(signedIn)) throw new StationSessionRefusedError("not-station");
  return signedIn;
}

/**
 * Remembers a successful lock: the page holds it from then on, and a second `ifAvailable` request from the same page
 * would answer "busy" and turn the owner into a duplicate.
 */
function lockOnce(acquire: StationEntryDeps["acquireLock"]): StationEntryDeps["acquireLock"] {
  let owned = false;
  return async () => {
    if (!owned) owned = await acquire();
    return owned;
  };
}

/**
 * Runs the table until it settles, publishing every state it passes through.
 * `offline` waits out `retryInMs` and tries again, like the desktop started without a network;
 * the station lock is requested once per run and kept while it retries.
 */
export async function runStationEntry(
  entryDeps: StationEntryDeps,
  publish: (state: StationEntryState) => void,
  signal: AbortSignal,
  sleep: (ms: number, signal: AbortSignal) => Promise<void> = abortableSleep,
): Promise<void> {
  const deps: StationEntryDeps = { ...entryDeps, acquireLock: lockOnce(entryDeps.acquireLock) };
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
