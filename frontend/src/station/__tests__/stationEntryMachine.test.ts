import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "../../api/auth";
import { ApiRequestError } from "../../api/core";
import { anonymousSession, authenticatedSession } from "../../__tests__/fixtures";
import {
  STATION_NETWORK_DENIED_CODE,
  STATION_RETRY_MAX_MS,
  STATION_RETRY_MIN_MS,
  StationSessionRefusedError,
  refreshStationSession,
  resolveStationEntry,
  retryDelayMs,
  runStationEntry,
  type StationEntryDeps,
  type StationEntryState,
} from "../entry/stationEntryMachine";

const stationSession: AuthSession = {
  authenticated: true,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  expires_at: "2030-01-01T00:00:00Z",
  csrf_token: "station-csrf",
};

function deps(overrides: Partial<StationEntryDeps> = {}): StationEntryDeps {
  return {
    getSession: vi.fn(async () => anonymousSession),
    login: vi.fn(async () => stationSession),
    acquireLock: vi.fn(async () => true),
    ...overrides,
  };
}

function apiError(status: number, code = "", retryAfterSeconds = 0) {
  return new ApiRequestError(status, "", "", code, retryAfterSeconds);
}

describe("resolveStationEntry", () => {
  it("opens the station when the session already belongs to it, without signing in", async () => {
    const d = deps({ getSession: vi.fn(async () => stationSession) });

    await expect(resolveStationEntry(d)).resolves.toEqual({
      kind: "ready",
      session: stationSession,
      csrfToken: "station-csrf",
    });
    expect(d.login).not.toHaveBeenCalled();
  });

  it.each(["admin", "operator", "warehouse", ""])("sends role %j to /admin and never replaces its cookie", async (role) => {
    const d = deps({ getSession: vi.fn(async () => ({ ...authenticatedSession, role })) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "redirect-admin" });
    expect(d.login).not.toHaveBeenCalled();
  });

  it("signs in when there is no session and opens the station on 200", async () => {
    const d = deps();

    await expect(resolveStationEntry(d)).resolves.toEqual({
      kind: "ready",
      session: stationSession,
      csrfToken: "station-csrf",
    });
    expect(d.login).toHaveBeenCalledTimes(1);
  });

  it("sends the browser to /admin when the network is not the warehouse", async () => {
    const d = deps({ login: vi.fn(async () => { throw apiError(403, STATION_NETWORK_DENIED_CODE); }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "redirect-admin" });
  });

  it.each([
    ["403 with another code", apiError(403, "origin_denied")],
    ["500", apiError(500)],
    ["503 station user unavailable", apiError(503)],
    ["network error", new TypeError("Failed to fetch")],
    ["429 without Retry-After", apiError(429)],
  ])("goes offline and retries in 30 s on %s", async (_name, failure) => {
    const d = deps({ login: vi.fn(async () => { throw failure; }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: STATION_RETRY_MIN_MS });
  });

  it("honours a longer Retry-After on 429 and ignores a shorter one", async () => {
    const longer = deps({ login: vi.fn(async () => { throw apiError(429, "", 120); }) });
    const shorter = deps({ login: vi.fn(async () => { throw apiError(429, "", 5); }) });

    await expect(resolveStationEntry(longer)).resolves.toEqual({ kind: "offline", retryInMs: 120_000 });
    await expect(resolveStationEntry(shorter)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
  });

  it("does not take the lock when the session belongs to someone else", async () => {
    const d = deps({ getSession: vi.fn(async () => authenticatedSession) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "redirect-admin" });
    expect(d.acquireLock).not.toHaveBeenCalled();
  });

  it("does not sign in when there is no session and another window owns the station", async () => {
    const d = deps({ acquireLock: vi.fn(async () => false) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "duplicate-tab" });
    expect(d.login).not.toHaveBeenCalled();
  });

  it("takes the lock before it signs in", async () => {
    const order: string[] = [];
    const d = deps({
      acquireLock: vi.fn(async () => { order.push("lock"); return true; }),
      login: vi.fn(async () => { order.push("login"); return stationSession; }),
    });

    await resolveStationEntry(d);

    expect(order).toEqual(["lock", "login"]);
  });

  it("goes offline when the session check itself fails, without signing in", async () => {
    const d = deps({ getSession: vi.fn(async () => { throw new TypeError("Failed to fetch"); }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
    expect(d.login).not.toHaveBeenCalled();
  });

  it("does not build a screen on a 200 that is not a station session", async () => {
    const d = deps({ login: vi.fn(async () => ({ ...stationSession, role: "admin" })) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
  });
});

describe("refreshStationSession", () => {
  it("returns the session of the station with its current csrf token, without signing in", async () => {
    const d = deps({ getSession: vi.fn(async () => ({ ...stationSession, csrf_token: "fresh" })) });

    await expect(refreshStationSession(d)).resolves.toMatchObject({ role: "station", csrf_token: "fresh" });
    expect(d.login).not.toHaveBeenCalled();
  });

  it.each(["admin", "operator", "warehouse", ""])("refuses a session of role %j and never replaces its cookie", async (role) => {
    const d = deps({ getSession: vi.fn(async () => ({ ...authenticatedSession, role })) });

    await expect(refreshStationSession(d)).rejects.toMatchObject({
      name: "StationSessionRefusedError",
      reason: "other-role",
    });
    await expect(refreshStationSession(d)).rejects.toBeInstanceOf(StationSessionRefusedError);
    expect(d.login).not.toHaveBeenCalled();
  });

  it("signs in when there is no session and returns what the login answered", async () => {
    const d = deps({ login: vi.fn(async () => ({ ...stationSession, csrf_token: "after-login" })) });

    await expect(refreshStationSession(d)).resolves.toMatchObject({ role: "station", csrf_token: "after-login" });
    expect(d.login).toHaveBeenCalledTimes(1);
  });

  it("refuses a login answer that is not a station session", async () => {
    const d = deps({ login: vi.fn(async () => ({ ...stationSession, role: "admin" })) });

    await expect(refreshStationSession(d)).rejects.toMatchObject({
      name: "StationSessionRefusedError",
      reason: "not-station",
    });
  });

  it("lets a failure of the session check or of the login through as it is", async () => {
    const down = new TypeError("Failed to fetch");
    const denied = apiError(403, STATION_NETWORK_DENIED_CODE);

    await expect(refreshStationSession(deps({ getSession: vi.fn(async () => { throw down; }) }))).rejects.toBe(down);
    await expect(refreshStationSession(deps({ login: vi.fn(async () => { throw denied; }) }))).rejects.toBe(denied);
  });
});

describe("retryDelayMs", () => {
  it("only lets Retry-After lengthen a 429", () => {
    expect(retryDelayMs(apiError(503, "", 600))).toBe(30_000);
    expect(retryDelayMs(apiError(429, "", 600))).toBe(600_000);
    expect(retryDelayMs(new Error("x"))).toBe(30_000);
  });

  it("caps a Retry-After of a day at ten minutes, so the timer can neither overflow nor sleep for a shift", () => {
    expect(STATION_RETRY_MAX_MS).toBe(600_000);
    expect(retryDelayMs(apiError(429, "", 86_400))).toBe(STATION_RETRY_MAX_MS);
    expect(retryDelayMs(apiError(429, "", 10_000_000))).toBe(STATION_RETRY_MAX_MS);
  });
});

describe("runStationEntry", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function recorder() {
    const states: StationEntryState[] = [];
    return { states, publish: (state: StationEntryState) => states.push(state) };
  }

  it("publishes checking, then ready, and takes the station lock once", async () => {
    const { states, publish } = recorder();
    const d = deps();

    await runStationEntry(d, publish, new AbortController().signal);

    expect(states.map((state) => state.kind)).toEqual(["checking", "ready"]);
    expect(d.acquireLock).toHaveBeenCalledTimes(1);
  });

  it("shows duplicate-tab and never reaches ready when another window holds the lock of a live station session", async () => {
    const { states, publish } = recorder();
    const d = deps({ getSession: vi.fn(async () => stationSession), acquireLock: vi.fn(async () => false) });

    await runStationEntry(d, publish, new AbortController().signal);

    expect(states.map((state) => state.kind)).toEqual(["checking", "duplicate-tab"]);
  });

  it("never calls login from the window that lost the lock, and never publishes ready", async () => {
    const { states, publish } = recorder();
    const d = deps({ acquireLock: vi.fn(async () => false) });

    await runStationEntry(d, publish, new AbortController().signal);

    expect(d.login).not.toHaveBeenCalled();
    expect(states.map((state) => state.kind)).toEqual(["checking", "duplicate-tab"]);
  });

  it("does not touch the lock for a session of another role", async () => {
    const { states, publish } = recorder();
    const d = deps({ getSession: vi.fn(async () => authenticatedSession) });

    await runStationEntry(d, publish, new AbortController().signal);

    expect(d.acquireLock).not.toHaveBeenCalled();
    expect(states.map((state) => state.kind)).toEqual(["checking", "redirect-admin"]);
  });

  it("asks for the lock once per run, however many times it retries while offline", async () => {
    const { states, publish } = recorder();
    const login = vi.fn<StationEntryDeps["login"]>()
      .mockRejectedValueOnce(apiError(503))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(stationSession);
    // A real Web Lock with ifAvailable, asked twice from the same page, answers "busy" the second time.
    const acquireLock = vi.fn<StationEntryDeps["acquireLock"]>()
      .mockResolvedValueOnce(true)
      .mockResolvedValue(false);

    await runStationEntry(
      deps({ login, acquireLock }),
      publish,
      new AbortController().signal,
      async () => undefined,
    );

    expect(acquireLock).toHaveBeenCalledTimes(1);
    expect(login).toHaveBeenCalledTimes(3);
    expect(states.map((state) => state.kind)).toEqual(["checking", "offline", "offline", "ready"]);
  });

  it("waits out the retry delay while offline and then opens the station", async () => {
    const { states, publish } = recorder();
    const sleeps: number[] = [];
    const login = vi.fn<StationEntryDeps["login"]>()
      .mockRejectedValueOnce(apiError(503))
      .mockRejectedValueOnce(apiError(429, "", 90))
      .mockResolvedValueOnce(stationSession);

    await runStationEntry(
      deps({ login }),
      publish,
      new AbortController().signal,
      async (ms) => { sleeps.push(ms); },
    );

    expect(states.map((state) => state.kind)).toEqual(["checking", "offline", "offline", "ready"]);
    expect(sleeps).toEqual([30_000, 90_000]);
    expect(login).toHaveBeenCalledTimes(3);
  });

  it("really sleeps 30 s between attempts and stops when the window goes away", async () => {
    vi.useFakeTimers();
    const { states, publish } = recorder();
    const controller = new AbortController();
    const login = vi.fn(async () => { throw apiError(503); });

    const running = runStationEntry(deps({ login }), publish, controller.signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(login).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(29_999);
    expect(login).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(login).toHaveBeenCalledTimes(2);

    controller.abort();
    await running;
    await vi.advanceTimersByTimeAsync(120_000);

    expect(login).toHaveBeenCalledTimes(2);
    expect(states.at(-1)?.kind).toBe("offline");
  });
});
