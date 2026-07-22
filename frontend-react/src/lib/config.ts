const envBase = import.meta.env.VITE_API_BASE as string | undefined;

export const API_BASE = (envBase && envBase.trim().replace(/\/$/, "")) || "http://localhost:8000";
