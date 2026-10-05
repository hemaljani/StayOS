'use client';

import { Suspense } from 'react';
import BriefDetail from './BriefDetail';

export default function BriefDetailPage() {
  return (
    <Suspense fallback={<div className="py-8 text-center text-gray-400">Loading brief...</div>}>
      <BriefDetail />
    </Suspense>
  );
}
