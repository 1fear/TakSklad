export type AppSurface = "operator" | "admin";
export type AdminWorkspaceTab =
  | "table"
  | "calendar"
  | "clients"
  | "smartup"
  | "imports"
  | "skladbotDryRun"
  | "incidents"
  | "activity";

export function resolveAppSurface(pathname: string): AppSurface {
  return pathname === "/admin" || pathname.startsWith("/admin/") ? "admin" : "operator";
}

export function hasOperatorSurfaceAccess(permissions: string[]): boolean {
  return permissions.includes("warehouse:read");
}

export function accessibleAdminTabsForPermissions(permissions: string[]): AdminWorkspaceTab[] {
  const tabs: AdminWorkspaceTab[] = [];
  if (permissions.includes("admin:read")) tabs.push("table");
  if (permissions.includes("client_points:read")) tabs.push("calendar", "clients");
  if (permissions.includes("admin:read")) tabs.push("smartup");
  if (permissions.includes("imports:read")) tabs.push("imports");
  if (permissions.includes("admin:read")) tabs.push("skladbotDryRun", "incidents", "activity");
  return tabs;
}

export function hasAdminSurfaceAccess(permissions: string[]): boolean {
  return accessibleAdminTabsForPermissions(permissions).length > 0;
}

export function surfacePath(surface: AppSurface): string {
  return surface === "admin" ? "/admin" : "/";
}

export function alternateSurfacePath(surface: AppSurface): string {
  return surfacePath(surface === "admin" ? "operator" : "admin");
}

export function surfaceTitle(surface: AppSurface): string {
  return surface === "admin" ? "Панель управления" : "Складская web-панель";
}
