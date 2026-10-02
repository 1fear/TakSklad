import { useEffect, useMemo, useState } from "react";

import { getAuthSession, stationLogin } from "../../api/auth";
import { defaultApiUrl, type ApiConfig } from "../../api/core";
import { acquireStationLock } from "../queue/stationLock";
import StationApp from "../StationApp";
import { runStationEntry, type StationEntryState } from "./stationEntryMachine";

/** What "/" shows. Status elements stay neutral until the station window gets its desktop status line. */
export default function StationEntry() {
  const [state, setState] = useState<StationEntryState>({ kind: "checking" });
  const csrfToken = state.kind === "ready" ? state.csrfToken : "";
  const stationConfig = useMemo<ApiConfig>(() => ({ apiUrl: defaultApiUrl(), token: "", csrfToken }), [csrfToken]);

  useEffect(() => {
    const controller = new AbortController();
    const config: ApiConfig = { apiUrl: defaultApiUrl(), token: "", csrfToken: "" };
    void runStationEntry(
      {
        getSession: () => getAuthSession(config, controller.signal),
        login: () => stationLogin(config),
        acquireLock: () => acquireStationLock(),
      },
      setState,
      controller.signal,
    );
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (state.kind === "redirect-admin") window.location.replace("/admin");
  }, [state.kind]);

  switch (state.kind) {
    case "ready":
      return <StationApp session={state.session} config={stationConfig} />;
    case "offline":
      return <div data-testid="station-offline" />;
    case "duplicate-tab":
      return <div data-testid="station-duplicate-tab" />;
    case "redirect-admin":
      return null;
    case "checking":
      return <div data-testid="station-checking" />;
  }
}
