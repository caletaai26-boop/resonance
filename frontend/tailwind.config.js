/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#0a0c11", 900: "#0e1118", 850: "#131722", 800: "#181d2a", 700: "#222838", 600: "#2e3548", 500: "#414a61" },
        ivory: { DEFAULT: "#f1e9d8", dim: "#c9c1b0", mute: "#8e8a80" },
        copper: { DEFAULT: "#c98a5a", deep: "#8f5632", soft: "#e0ab82" },
        champagne: "#e6d3a8",
        sage: "#8fb69a",
        rose: "#d98880",
        amber: "#e0b25c",
      },
      fontFamily: {
        display: ['"Iowan Old Style"', '"Palatino Linotype"', "Palatino", '"Book Antiqua"', "Georgia", "serif"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: { panel: "0 1px 0 0 rgba(241,233,216,0.04) inset, 0 12px 40px -20px rgba(0,0,0,0.8)" },
    },
  },
  plugins: [],
};
