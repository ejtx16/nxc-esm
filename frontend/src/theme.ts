import { createSystem, defaultConfig, defineConfig } from "@chakra-ui/react";

/**
 * ESM Ops theme. Cool mineral ground, ink navy, and one amber accent.
 * Chakra's own semantic tokens (bg, bg.panel, fg.muted, border) are remapped so every
 * built-in component picks up the look without per-component overrides.
 */
const config = defineConfig({
  globalCss: {
    "html, body, #root": { height: "100%" },
    body: { bg: "bg", color: "fg", fontVariantNumeric: "tabular-nums" },
  },
  theme: {
    tokens: {
      fonts: {
        heading: { value: '"Inter Variable", Inter, system-ui, -apple-system, "Segoe UI", Arial, sans-serif' },
        body: { value: '"Inter Variable", Inter, system-ui, -apple-system, "Segoe UI", Arial, sans-serif' },
      },
      colors: {
        ink: { value: "#13222b" },
        barrier: { value: "#f2b705" },
      },
      radii: { l1: { value: "4px" }, l2: { value: "4px" }, l3: { value: "6px" } },
    },
    semanticTokens: {
      colors: {
        bg: { DEFAULT: { value: "#e6eaee" }, panel: { value: "#f6f8f9" }, subtle: { value: "#edf0f3" }, muted: { value: "#dce2e7" } },
        fg: { DEFAULT: { value: "#13222b" }, muted: { value: "#47596a" }, subtle: { value: "#6f7f8b" } },
        border: { DEFAULT: { value: "#c6cfd6" }, muted: { value: "#d8dee3" }, emphasized: { value: "#6f7f8b" } },
        // The default button/switch/progress palette is ink instead of near-black grey.
        gray: { solid: { value: "#13222b" }, contrast: { value: "#ffffff" } },
      },
    },
  },
});

export const system = createSystem(defaultConfig, config);
