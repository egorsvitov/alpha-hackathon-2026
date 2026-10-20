import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// VITE_API_MODE=mock (по умолчанию) | http; VITE_API_URL — адрес backend для режима http.
export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 5173 },
  preview: { host: true, port: 4173 },
});
