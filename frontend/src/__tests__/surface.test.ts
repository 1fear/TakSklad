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

  it("opens the markings tab only when admin:read and reports:read are both granted", () => {
    expect(accessibleAdminTabsForPermissions(["reports:read"])).toEqual([]);
    expect(accessibleAdminTabsForPermissions(["admin:read"])).not.toContain("kizDaily");
    expect(accessibleAdminTabsForPermissions(["admin:read", "reports:read"])).toContain("kizDaily");
    expect(hasAdminSurfaceAccess(["reports:read"])).toBe(false);
  });
});
