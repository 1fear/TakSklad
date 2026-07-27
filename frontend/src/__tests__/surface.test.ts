import { describe, expect, it } from "vitest";

import {
  accessibleAdminTabsForPermissions,
  hasAdminSurfaceAccess,
  hasOperatorSurfaceAccess,
  resolveAppSurface,
} from "../workspace/surface";

describe("surface helper characterization", () => {
  it("routes root to operator and /admin paths to admin", () => {
    expect(resolveAppSurface("/")).toBe("operator");
    expect(resolveAppSurface("/orders")).toBe("operator");
    expect(resolveAppSurface("/admin")).toBe("admin");
    expect(resolveAppSurface("/admin/incidents")).toBe("admin");
  });

  it("fails closed for operator access without warehouse:read", () => {
    expect(hasOperatorSurfaceAccess(["warehouse:read"])).toBe(true);
    expect(hasOperatorSurfaceAccess(["warehouse:write"])).toBe(false);
    expect(hasOperatorSurfaceAccess([])).toBe(false);
  });

  it("treats imports and client points as valid admin sections without admin:read", () => {
    expect(accessibleAdminTabsForPermissions(["imports:read"])).toEqual(["imports"]);
    expect(accessibleAdminTabsForPermissions(["client_points:read"])).toEqual(["calendar", "clients"]);
    expect(hasAdminSurfaceAccess(["imports:read"])).toBe(true);
    expect(hasAdminSurfaceAccess(["client_points:read"])).toBe(true);
    expect(hasAdminSurfaceAccess(["admin:write"])).toBe(false);
  });
});
