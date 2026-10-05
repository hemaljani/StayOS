// OnboardingTour - first-login coachmark for the StayOS shell launcher.
//
// A lightweight two-step guided tour shown the first time a GM signs in (gated
// by useOnboarding). Step 1 highlights the LUMI card and explains the daily
// brief; clicking the bubble advances to step 2, which highlights PULSE and
// explains real-time alerts; clicking again finishes. A dimming overlay focuses
// attention on the highlighted card, and a Skip control dismisses the whole
// tour. Copy is grounded in the LUMI/PULSE PRFAQs (GM benefits).
//
// Positioning: the bubble is placed just below the target card and horizontally
// aligned to it, measured from the target element's rect relative to the shared
// positioning container. It re-measures on step change and on resize so it
// stays anchored as the layout reflows. The component renders nothing until it
// has a measured position, avoiding a flash in the wrong place.

'use client';

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

// One step of the tour: which card it points at, plus its teaching copy.
interface TourStep {
  id: string;
  // Ref to the highlighted card element for this step.
  targetRef: React.RefObject<HTMLElement | null>;
  title: string;
  body: string;
}

interface OnboardingTourProps {
  // The steps to walk through, in order (LUMI then PULSE).
  steps: TourStep[];
  // Element the bubble is positioned within (the grid's positioning context).
  containerRef: React.RefObject<HTMLElement | null>;
  // Called when the tour finishes or is skipped (persists the seen-flag).
  onFinish: () => void;
}

// Measured bubble placement, relative to the container's top-left.
interface BubblePosition {
  top: number;
  // Horizontal center the bubble + arrow align to.
  centerX: number;
}

export default function OnboardingTour({ steps, containerRef, onFinish }: OnboardingTourProps) {
  const [stepIndex, setStepIndex] = useState(0);
  const [position, setPosition] = useState<BubblePosition | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);

  const step = steps[stepIndex];
  const isLastStep = stepIndex === steps.length - 1;

  // Measure the current target relative to the container and place the bubble
  // just beneath it, centered on the card.
  const measure = useCallback(() => {
    const container = containerRef.current;
    const target = step?.targetRef.current;
    if (!container || !target) return;
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    setPosition({
      top: targetRect.bottom - containerRect.top + 12,
      centerX: targetRect.left - containerRect.left + targetRect.width / 2,
    });
  }, [containerRef, step]);

  // Re-measure synchronously before paint on step change so the bubble never
  // paints at a stale position, and on resize so it tracks layout reflow.
  useLayoutEffect(() => {
    measure();
  }, [measure]);

  useEffect(() => {
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [measure]);

  // Highlight the active target card with a bright accent ring while the tour
  // runs, and restore it on cleanup / step change. A ring (not a z-index lift)
  // keeps the highlight predictable regardless of stacking context.
  useEffect(() => {
    const target = step?.targetRef.current;
    if (!target) return;
    const HIGHLIGHT_CLASSES = ['ring-2', 'ring-accent', 'ring-offset-2', 'ring-offset-background'];
    target.classList.add(...HIGHLIGHT_CLASSES);
    return () => target.classList.remove(...HIGHLIGHT_CLASSES);
  }, [step]);

  const advance = useCallback(() => {
    if (isLastStep) {
      onFinish();
      return;
    }
    setStepIndex((index) => index + 1);
  }, [isLastStep, onFinish]);

  if (!step) return null;

  return (
    <>
      {/* Dimming overlay. Clicking it advances the tour (same as the bubble).
          Fixed so it covers the whole viewport regardless of the positioning
          container the bubble is measured against. */}
      <div
        className="fixed inset-0 z-20 bg-ink/40 backdrop-blur-[1px]"
        aria-hidden
        onClick={advance}
      />

      {/* Coachmark bubble, anchored beneath the highlighted card. */}
      {position && (
        <div
          ref={bubbleRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="onboarding-title"
          className="absolute z-40 w-64 -translate-x-1/2 cursor-pointer rounded-xl border border-accent/40 bg-surface p-4 shadow-xl shadow-accent/10"
          style={{ top: position.top, left: position.centerX }}
          onClick={advance}
        >
          {/* Arrow pointing up at the card. */}
          <div className="absolute -top-2 left-1/2 h-4 w-4 -translate-x-1/2 rotate-45 border-l border-t border-accent/40 bg-surface" />

          <div className="flex items-center justify-between">
            <span className="text-[10px] font-medium uppercase tracking-wide text-accent">
              Step {stepIndex + 1} of {steps.length}
            </span>
            {/* Skip dismisses the whole tour without advancing. */}
            <button
              type="button"
              onClick={(event) => {
                // Do not also trigger the bubble's advance handler.
                event.stopPropagation();
                onFinish();
              }}
              className="text-[10px] text-ink-faint hover:text-ink-soft"
            >
              Skip
            </button>
          </div>

          <h2 id="onboarding-title" className="mt-1 text-sm font-semibold text-ink">
            {step.title}
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-ink-soft">{step.body}</p>

          <div className="mt-3 flex items-center justify-between">
            {/* Step dots. */}
            <div className="flex gap-1">
              {steps.map((tourStep, index) => (
                <span
                  key={tourStep.id}
                  className={`h-1.5 w-1.5 rounded-full ${
                    index === stepIndex ? 'bg-accent' : 'bg-line'
                  }`}
                />
              ))}
            </div>
            <span className="text-xs font-semibold text-accent">
              {isLastStep ? 'Got it' : 'Next'}
            </span>
          </div>
        </div>
      )}
    </>
  );
}
