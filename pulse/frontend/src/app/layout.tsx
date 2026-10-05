// Root layout for the PULSE PWA.
//
// Mirrors LUMI's shell: an error boundary wraps an auth guard, which wraps the
// fixed header, the routed page content, and the four-tab bottom nav. The
// service-worker registrar and the token-refresh timer run app-wide. PWA
// metadata (manifest, theme color, apple-web-app) makes the app installable.

import type { Metadata, Viewport } from 'next';
import '@/styles/globals.css';
import BottomNav from '@/components/BottomNav';
import AppHeader from '@/components/AppHeader';
import ServiceWorkerRegister from '@/components/ServiceWorkerRegister';
import TokenRefreshProvider from '@/components/TokenRefreshProvider';
import AuthGuard from '@/components/AuthGuard';
import ErrorBoundary from '@/components/ErrorBoundary';

export const metadata: Metadata = {
  title: 'PULSE | StayOS',
  description: 'Real-time tiered alerts for hotel General Managers - Part of StayOS',
  manifest: '/manifest.json',
  appleWebApp: {
    capable: true,
    statusBarStyle: 'black-translucent',
    title: 'PULSE',
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  viewportFit: 'cover',
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#eef3f7' },
    { media: '(prefers-color-scheme: dark)', color: '#0f1722' },
  ],
};

// No-flash theme bootstrap: applies the StayOS-wide shared theme preference
// No-flash theme bootstrap: applies the StayOS-wide shared theme preference
// (localStorage 'stayos-theme') before paint. Dark default for new users - light
// applies ONLY when explicitly saved. Shared across apps.
const THEME_INIT = `(function(){try{var t=localStorage.getItem('stayos-theme');if(t!=='light'){document.documentElement.classList.add('dark');}}catch(e){document.documentElement.classList.add('dark');}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* basePath does not rewrite raw <link> hrefs, so these static asset
            paths are prefixed with /pulse explicitly (matches next.config.js). */}
        <link rel="icon" href="/pulse/favicon.svg" type="image/svg+xml" />
        <link rel="apple-touch-icon" href="/pulse/icons/icon-192.svg" />
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="bg-background text-ink min-h-screen">
        <ErrorBoundary>
          <AuthGuard>
            <AppHeader />
            <main className="pt-12 pb-16 px-4 max-w-md mx-auto min-h-screen">
              {children}
            </main>
            <BottomNav />
          </AuthGuard>
        </ErrorBoundary>
        <ServiceWorkerRegister />
        <TokenRefreshProvider />
      </body>
    </html>
  );
}
