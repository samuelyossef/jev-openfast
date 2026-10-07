import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../jev_ultrafast/web', emptyOutDir: true },
});
