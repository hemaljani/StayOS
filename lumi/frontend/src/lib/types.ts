export interface BriefResponse {
  property: Property;
  dailyKPIs: DailyKPIs;
  actionItems: ActionItem[];
  vipArrivals: VipArrival[];
  audioBrief: AudioBrief;
}

export interface Property {
  propertyId: string;
  propertyName: string;
  brand: string;
  timezone: string;
  totalRooms: number;
}

export interface DailyKPIs {
  date: string;
  asOf: string;
  occupancy: OccupancyKPI;
  adr: AdrKPI;
  revPAR: RevParKPI;
  arrivals: ArrivalsKPI;
  departures: DeparturesKPI;
  confirmedReservations: number;
  availableRooms: number;
}

export interface OccupancyKPI {
  current: number;
  unit: string;
  vsLastWeek: number;
  vsBudget: number;
  forecast3pm: number;
}

export interface AdrKPI {
  current: number;
  currency: string;
  vsLastWeek: number;
  vsBudget: number;
  pacePctOfBudget: number;
}

export interface RevParKPI {
  current: number;
  currency: string;
  vsYOY: number;
  budget: number;
}

export interface ArrivalsKPI {
  total: number;
  vipCount: number;
  ambassadorCount: number;
  titaniumCount: number;
  platinumCount: number;
}

export interface DeparturesKPI {
  total: number;
  groupCheckouts: number;
  groupRooms: number;
}

export interface ActionItem {
  id: string;
  type: 'OVERBOOKING_RISK' | 'ROOMS_OUT_OF_ORDER' | 'VIP_ARRIVAL_ALERT' | 'UPSELL_OPPORTUNITY' | 'STAFFING_CONFIRMED';
  severity: 'URGENT' | 'HIGH' | 'MEDIUM' | 'LOW';
  title: string;
  detail: string;
  data: Record<string, any>;
  source: string;
  generatedAt: string;
}

export interface VipArrival {
  guestId: string;
  guestName: string;
  initials: string;
  loyaltyTier: 'AMBASSADOR' | 'TITANIUM' | 'PLATINUM';
  loyaltyNumber?: string;
  totalStays: number;
  roomNumber: string;
  roomType: string;
  estimatedArrival: string;
  specialOccasion: string | null;
  preferences: string[];
  accountType: 'PERSONAL' | 'CORPORATE';
  corporateAccount?: string;
  sensitiveNotes?: string[];
}

export interface AudioBrief {
  briefId: string;
  durationSeconds: number;
  status: 'READY' | 'GENERATING' | 'FAILED' | 'TEXT_ONLY';
  audioUrl?: string;
  transcriptSnippet?: string;
}

export interface GmSettings {
  gmAlias: string;
  gmName: string;
  propertyId: string;
  propertyName: string;
  briefDeliveryTime: string;
  alertToggles: AlertToggles;
  kpiThresholds: KpiThresholds;
  audioPreferences: AudioPreferences;
  createdAt?: string;
  updatedAt?: string;
}

export interface AlertToggles {
  overbookingRisk: boolean;
  roomsOutOfOrder: boolean;
  vipArrivalAlert: boolean;
  upsellOpportunity: boolean;
  staffingConfirmed: boolean;
}

export interface KpiThresholds {
  occupancyAlertBelow: number;
  adrAlertBelow: number;
}

export interface AudioPreferences {
  language: 'en-US' | 'es-ES' | 'ja-JP' | 'zh-CN';
  briefLength: 'brief' | 'standard' | 'detailed';
}

export interface ErrorResponse {
  error: {
    code: string;
    message: string;
    field?: string;
  };
}


export interface BriefHistorySummary {
  briefDate: string;
  status: string;
  dailyKPIs: {
    occupancy: { current: number };
    adr: { current: number };
    revPAR: { current: number };
  };
  narrativePreview: string;
}
