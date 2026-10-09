// Tailwind tokens for PULSE — "Coastal Blue", light + dark via CSS variables
// (shared StayOS identity). Token VALUES live in globals.css under :root (light)
// and .dark (Coastal Blue dark); tokens reference `rgb(var(--token) / <alpha-value>)`
// so opacity modifiers work in both themes. darkMode:'class' — the shared theme
// toggle sets/removes `dark` on <html> (preference shared across shell/LUMI/PULSE).
import type { Config } from 'tailwindcss';

const withAlpha = (v: string) => `rgb(var(${v}) / <alpha-value>)`;

const config: Config = {
  content: [
    './src/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        background: withAlpha('--c-background'),
        'background-2': withAlpha('--c-background-2'),
        surface: withAlpha('--c-surface'),
        'surface-2': withAlpha('--c-surface-2'),
        ink: withAlpha('--c-ink'),
        'ink-soft': withAlpha('--c-ink-soft'),
        'ink-faint': withAlpha('--c-ink-faint'),
        line: withAlpha('--c-line'),
        accent: withAlpha('--c-accent'),
        'accent-deep': withAlpha('--c-accent-deep'),
        'accent-secondary': withAlpha('--c-accent-secondary'),
        success: withAlpha('--c-success'),
        warning: withAlpha('--c-warning'),
        danger: withAlpha('--c-danger'),
        'tier-critical': withAlpha('--c-tier-critical'),
        'tier-warning': withAlpha('--c-tier-warning'),
        'tier-info': withAlpha('--c-tier-info'),
        'tier-ambassador': withAlpha('--c-warning'),
        'tier-titanium': withAlpha('--c-ink-soft'),
        'tier-platinum': withAlpha('--c-accent'),
        white: withAlpha('--c-white'),
        gray: {
          200: withAlpha('--c-gray-200'),
          300: withAlpha('--c-gray-300'),
          400: withAlpha('--c-gray-400'),
          500: withAlpha('--c-gray-500'),
          600: withAlpha('--c-gray-600'),
          700: withAlpha('--c-gray-700'),
          800: withAlpha('--c-gray-800'),
          900: withAlpha('--c-gray-900'),
        },
      },
      borderRadius: {
        lg: '8px',
        xl: '10px',
        '2xl': '12px',
      },
      keyframes: {
        'slide-up': {
          '0%': { transform: 'translateY(100%)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
      },
      animation: {
        'slide-up': 'slide-up 0.22s ease-out',
      },
    },
  },
  plugins: [],
};

export default config;
