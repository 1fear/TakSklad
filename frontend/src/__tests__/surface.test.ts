import { describe, expect, it } from "vitest";

import {
  accessibleAdminTabsForPermissions,
  alternateSurfacePath,
  hasAdminSurfaceAccess,
  hasOperatorSurfaceAccess,
  resolveAppSurface,
  surfacePath,
  surfaceOpenLabel,
  surfaceTitle,
} from "../workspace/surface";

describe("surface helper characterization", () => {
  it("routes /admin and below to admin and everything else to the station", () => {
    expect(resolveAppSurface("/")).toBe("station");
    expect(resolveAppSurface("/orders")).toBe("station");
    expect(resolveAppSurface("/administrator")).toBe("station");
    expect(resolveAppSurface("/admin")).toBe("admin");
    expect(resolveAppSurface("/admin/incidents")).toBe("admin");
  });

  it("maps each surface to its own path, the other surface and a title", () => {
    expect(surfacePath("station")).toBe("/");
    expect(surfacePath("admin")).toBe("/admin");
    expect(alternateSurfacePath("station")).toBe("/admin");
    expect(alternateSurfacePath("admin")).toBe("/");
    expect(surfaceTitle("station")).toBe("Складская web-панель");
    expect(surfaceTitle("admin")).toBe("Панель управления");
    expect(surfaceOpenLabel("station")).toBe("Открыть складскую web-панель");
    expect(surfaceOpenLabel("admin")).toBe("Открыть панель управления");
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
