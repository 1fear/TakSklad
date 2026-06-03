import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const devApiTarget = process.env.VITE_TAKSKLAD_DEV_API_URL?.replace(/\/$/, "");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: devApiTarget
      ? {
        "/api": {
          target: devApiTarget,
          changeOrigin: true,
          secure: true,
        },
      }
      : undefined,
  },
});
