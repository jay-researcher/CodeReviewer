import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../../code_reviewer/static',
    emptyOutDir: false,
    lib: {
      entry: 'src/main.tsx',
      name: 'CodeReviewerADF',
      formats: ['iife'],
      fileName: () => 'adf-editor.js'
    }
  }
});
