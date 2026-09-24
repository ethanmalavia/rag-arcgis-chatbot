import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
// VITE_BASE=/rag-arcgis-chatbot/ for GitHub Pages project site; "/" for local/Cloud Run.
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE || '/',
})
