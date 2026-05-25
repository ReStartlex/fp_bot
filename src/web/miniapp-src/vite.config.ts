import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Mini App build → ../miniapp/, чтобы FastAPI отдавал из src/web/miniapp/.
// base='./' критично: Telegram WebView грузит файлы по относительным
// путям, иначе ассеты ломаются при mount под /app/.
export default defineConfig({
    plugins: [preact()],
    base: "./",
    build: {
        outDir: "../miniapp",
        emptyOutDir: true,
        sourcemap: false,
        target: "es2020",
    },
    server: {
        port: 5173,
        proxy: {
            // Vite dev → проксируем /api на FastAPI 8000 (или твой порт)
            // чтобы не возиться с CORS локально.
            "/api": "http://localhost:8000",
        },
    },
});
