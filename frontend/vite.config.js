import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [
    vue(),
    {
      name: "local-site-access",
      configureServer(server) {
        server.middlewares.use("/site-access.json", (_request, response) => {
          response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
          response.end('{"maintenance":true}');
        });
      },
    },
    {
      name: "maintenance-business-styles",
      apply: (config, env) => env.command === "build" && !config.build?.ssr,
      enforce: "post",
      generateBundle(_options, bundle) {
        const entry = Object.values(bundle).find(
          (asset) => asset.type === "chunk" && asset.isEntry && asset.name === "index",
        );
        const files = Array.from(entry?.viteMetadata?.importedCss ?? []);
        if (!files.length) this.error("Missing business styles for the maintenance page");
        this.emitFile({
          type: "asset",
          fileName: "maintenance-ui.css",
          source: files.map((file) => `@import url("/${file}");`).join("\n") + "\n",
        });
      },
    },
  ],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
