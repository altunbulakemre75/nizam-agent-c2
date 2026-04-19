import { defineConfig } from "vite";
import cesium from "vite-plugin-cesium";

export default defineConfig({
  plugins: [cesium()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/ws": {
        target: "ws://localhost:8200",
        ws: true,
      },
    },
  },
  build: {
    target: "es2022",
    outDir: "dist",
  },
});
