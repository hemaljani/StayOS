// StayOSLogo - the StayOS platform lockup (mark + wordmark).
//
// A single reusable lockup shared by the shell's login view and the feature
// launcher so the brand stays consistent. Renders the platform mark
// (public/logo.svg - an OS-window tile holding a rising spectrum arc) above the
// "StayOS" wordmark, with "OS" tinted in the accent gradient to reinforce
// "operating system". The shell serves from the site root (basePath unset), so
// the raw "/logo.svg" path is correct here.

interface StayOSLogoProps {
  // Pixel size of the square logo mark. Wordmark scales via `size` prop below.
  size?: number;
  // Tailwind font-size class for the wordmark (e.g. 'text-3xl', 'text-2xl').
  wordmarkClassName?: string;
}

export default function StayOSLogo({
  size = 56,
  wordmarkClassName = 'text-3xl',
}: StayOSLogoProps) {
  return (
    <div className="flex flex-col items-center">
      {/* Platform mark. Decorative here - the wordmark provides the accessible name. */}
      <img src="/logo.svg" alt="" width={size} height={size} className="mb-3" aria-hidden />
      <h1 className={`${wordmarkClassName} font-bold tracking-wide leading-none`}>
        <span className="text-ink">Stay</span>
        <span className="bg-gradient-to-r from-accent to-accent-deep bg-clip-text text-transparent">
          OS
        </span>
      </h1>
    </div>
  );
}
