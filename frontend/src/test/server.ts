import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Outside the warehouse network the station sign-in is refused; tests that need it open override this.
export const server = setupServer(
  http.post("/api/v1/auth/station", () => HttpResponse.json(
    { detail: { code: "station_network_denied" } },
    { status: 403, statusText: "Forbidden" },
  )),
);
