import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";
import {resolve} from "node:path";

export default defineConfig({
  root: resolve(__dirname, "frontend"),
  base: "/vnext/",
  plugins: [react()],
  build: {
    outDir: resolve(__dirname, "vnext"),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {port: 4173, strictPort: true},
});
