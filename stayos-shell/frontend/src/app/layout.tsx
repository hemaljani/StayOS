import type { Metadata, Viewport } from 'next';
import '@/styles/globals.css';

// StayOS shell metadata. The shell is the StayOS entry point (login + feature
// launcher) served at the site root.
export const metadata: Metadata = {
  title: 'StayOS',
  description: 'The Operating System for Hotel Associates',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  viewportFit: 'cover',
  // Theme-aware browser chrome color: light canvas / dark slate-navy.
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#eef3f7' },
    { media: '(prefers-color-scheme: dark)', color: '#0f1722' },
  ],
};

// No-flash theme bootstrap: runs before paint, applies the StayOS-wide shared
// theme preference (localStorage key 'stayos-theme') by adding `dark` to <html>.
// Dark is the default for new users - light applies ONLY when explicitly saved.
// Shared across shell/LUMI/PULSE.
const THEME_INIT = `(function(){try{var t=localStorage.getItem('stayos-theme');if(t!=='light'){document.documentElement.classList.add('dark');}}catch(e){document.documentElement.classList.add('dark');}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="bg-background text-ink min-h-screen">
        <main className="px-4 w-full max-w-lg mx-auto min-h-screen">{children}</main>
      </body>
    </html>
  );
}
