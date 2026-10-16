import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
// Leave VITE_BASE unset so assets resolve at "/" (local and VPS).
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE || '/',
})
