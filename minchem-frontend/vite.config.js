import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on 5173: the backend's CORS list allows http://localhost:5173
// and http://127.0.0.1:5173 only.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true }
});
