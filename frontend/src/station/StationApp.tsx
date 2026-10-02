import type { AuthSession } from "../api/auth";
import type { ApiConfig } from "../api/core";

export type StationAppProps = {
  session: AuthSession;
  config: ApiConfig;
};

/** Placeholder: the real station window arrives in a later plan. */
export default function StationApp(_props: StationAppProps) {
  return <div data-testid="station-app" />;
}
