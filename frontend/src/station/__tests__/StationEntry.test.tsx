import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { anonymousSession, authenticatedSession } from "../../__tests__/fixtures";
import type { AuthSession } from "../../api/auth";
import { server } from "../../test/server";
import StationEntry from "../entry/StationEntry";
import { acquireStationLock } from "../queue/stationLock";
import { fakeLocks } from "./webLocks";

const stationSession = {
  ...authenticatedSession,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  csrf_token: "station-csrf",
};

let replace: ReturnType<typeof vi.fn>;
let stationLogins: number;

beforeEach(() => {
  replace = vi.fn();
  vi.stubGlobal("location", { ...window.location, replace });
  stationLogins = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "locks");
});

function session(body: AuthSession) {
  server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(body)));
}

function stationLogin(respond: () => Response) {
  server.use(http.post("/api/v1/auth/station", () => {
    stationLogins += 1;
    return respond();
  }));
}

describe("StationEntry", () => {
  it("opens the station window for a station session without signing in again", async () => {
    session(stationSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    expect(screen.getByTestId("station-checking")).toBeInTheDocument();
    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
    expect(stationLogins).toBe(0);
    expect(replace).not.toHaveBeenCalled();
  });

  it("signs in from the warehouse network when there is no session", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
    expect(stationLogins).toBe(1);
  });

  it("leaves an admin session alone and goes to /admin", async () => {
    session(authenticatedSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
    expect(stationLogins).toBe(0);
    expect(screen.queryByTestId("station-app")).not.toBeInTheDocument();
  });

  it("goes to /admin outside the warehouse network", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json({ detail: { code: "station_network_denied" } }, { status: 403 }));

    render(<StationEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
    expect(replace).toHaveBeenCalledTimes(1);
  });

  it("stays on a neutral offline element when the backend cannot sign the station in", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json({}, { status: 503 }));

    render(<StationEntry />);

    expect(await screen.findByTestId("station-offline")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    expect(stationLogins).toBe(1);
  });

  it("shows duplicate-tab when another window already owns the station", async () => {
    session(stationSession);
    const { locks } = fakeLocks();
    Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
    await expect(acquireStationLock(locks)).resolves.toBe(true);

    render(<StationEntry />);

    expect(await screen.findByTestId("station-duplicate-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("station-app")).not.toBeInTheDocument();
  });

  it("does not sign in from a window that lost the lock, so the owner keeps its cookie", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json(stationSession));
    const { locks } = fakeLocks();
    Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
    await expect(acquireStationLock(locks)).resolves.toBe(true);

    render(<StationEntry />);

    expect(await screen.findByTestId("station-duplicate-tab")).toBeInTheDocument();
    expect(stationLogins).toBe(0);
  });
});
