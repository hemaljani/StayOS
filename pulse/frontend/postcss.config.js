// PostCSS configuration for the PULSE PWA (mirrors LUMI).
// Runs Tailwind to expand the utility layers and Autoprefixer for cross-browser
// vendor prefixes during the Next.js build.
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
