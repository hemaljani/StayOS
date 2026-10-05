// StayOS feature catalog rendered by the shell's launcher grid.
//
// Ported from LUMI's former landing page. `active` cards are clickable and link
// out to that feature's app on the shared StayOS origin (LUMI at /lumi, PULSE at
// /pulse); inactive cards render as "Coming Soon". The shell owns this catalog
// now that it is the single StayOS entry point.

import {
  BarChart3,
  Users,
  Award,
  BriefcaseBusiness,
  Sun,
  Zap,
  type LucideIcon,
} from 'lucide-react';

export interface Feature {
  id: string;
  name: string;
  description: string;
  Icon: LucideIcon;
  active: boolean;
  // Absolute path on the shared StayOS origin for active features. Omitted for
  // "Coming Soon" cards.
  href?: string;
}

export const FEATURES: Feature[] = [
  {
    id: 'lumi',
    name: 'LUMI',
    description:
      'GM Daily Intelligence Brief - AI-powered morning brief with KPIs, VIPs, and action items',
    Icon: Sun,
    active: true,
    href: '/lumi/',
  },
  {
    id: 'pulse',
    name: 'PULSE',
    description:
      'Real-time tiered alerts for walk risk, VIP room readiness, and escalating situations throughout the shift',
    Icon: Zap,
    active: true,
    href: '/pulse/',
  },
  {
    id: 'revenue-optimizer',
    name: 'Revenue Optimizer',
    description: 'Rate elasticity, segment mix, and upsell signals for revenue managers',
    Icon: BarChart3,
    active: false,
  },
  {
    id: 'guest-experience',
    name: 'Guest Experience',
    description: 'Preference and loyalty-tier patterns for front desk and guest services',
    Icon: Users,
    active: false,
  },
  {
    id: 'best-practice-coach',
    name: 'Best Practice Coach',
    description: 'AI coaching nudges based on property performance vs. peer benchmarks',
    Icon: Award,
    active: false,
  },
  {
    id: 'portfolio-analyzer',
    name: 'Portfolio Analyzer',
    description: 'Cross-property rollup and comparative insights for Area Vice Presidents',
    Icon: BriefcaseBusiness,
    active: false,
  },
];
