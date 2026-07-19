import { apiRequest, type ApiConfig } from "./core";

export type DesktopPairingCreateRequest = {
  device_label?: string;
};

export type DesktopPairingCreateResponse = {
  pairing_id: string;
  setup_code: string;
  expires_at: string;
};

export async function createDesktopPairing(
  config: ApiConfig,
  deviceLabel: string,
): Promise<DesktopPairingCreateResponse> {
  const normalizedLabel = deviceLabel.trim();
  return apiRequest<DesktopPairingCreateResponse>(config, "/api/v1/admin/desktop-pairings", {
    method: "POST",
    body: normalizedLabel ? { device_label: normalizedLabel } : {},
  });
}
