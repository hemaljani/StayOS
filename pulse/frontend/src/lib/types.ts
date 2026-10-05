// TypeScript models for the PULSE PWA.
//
// These mirror the exact JSON the PULSE REST API returns for alert reads. Field
// names are the camelCase DynamoDB attribute names produced by the backend
// (see backend src/pulse/common/models.py and api/alerts_repository.py); they
// are NOT invented here. Enum string values match the backend StrEnum members.

// Alert severity classification (backend AlertTier).
export type AlertTier = 'CRITICAL' | 'WARNING' | 'INFO';

// Alert lifecycle state (backend AlertStatus). RESOLVED is terminal; resolved
// alerts are excluded from the live feed and shown only in resolved history
// (Requirement 12.4, Property 20).
export type AlertStatus =
  | 'UNACKNOWLEDGED'
  | 'ACKNOWLEDGED'
  | 'RESOLVED'
  | 'ESCALATED'
  | 'ESCALATION_EXHAUSTED';

// The eight MVP alert types (backend AlertType), plus the predictive
// FORECAST_OVERSELL type emitted by the Forecasting Agent. Listing it here lets
// alertTypeLabel() humanize it as "Forecast Oversell".
export type AlertType =
  | 'WALK_RISK'
  | 'VIP_ROOM_NOT_READY'
  | 'COMPLAINT_ESCALATION'
  | 'OOO_CLUSTER'
  | 'PREMIUM_CANCELLATION'
  | 'VIP_CHECKIN'
  | 'FORECAST_OVERSELL';

// Whether an alert has been flagged for mandatory GM review (backend EscalationStatus).
export type EscalationStatus = 'NONE' | 'MANDATORY_GM_REVIEW';

// Discrete review-risk level on a complaint ranked option (backend ReviewRisk).
export type ReviewRisk = 'Low' | 'Medium' | 'High';

// Human-approval gate state (backend ApprovalState).
export type ApprovalState = 'PENDING' | 'APPROVED' | 'REJECTED';

// A single GM-selectable action produced by the Triage Agent (backend RankedOption).
export interface RankedOption {
  label: string;
  rank: number;
  title: string;
  detail: string;
  recommended: boolean;
  estimatedCost?: number;
  reviewRisk?: ReviewRisk;
}

// A guest identified as walkable in a Walk_Strategy (backend WalkableGuest).
export interface WalkableGuest {
  guestId: string;
  loyaltyTier: string;
  reservationId: string;
}

// A drafted compensation package for a walkable guest (backend CompensationPackage).
export interface CompensationPackage {
  guestId: string;
  description: string;
  estimatedCost: number;
}

// The Walk_Strategy attached to a Walk Risk triage brief (backend WalkStrategy).
export interface WalkStrategy {
  sisterPropertyId: string | null;
  sisterPropertyAvailable: boolean;
  walkableGuests: WalkableGuest[];
  compensation: CompensationPackage[];
}

// Agent-generated decision package for an alert (backend TriageBrief).
// CRITICAL/WARNING briefs are actionable (approve -> executable write-back); the
// two INFO types (premium cancellation, VIP check-in) get an ADVISORY brief with
// ranked suggestions and no executable action.
export interface TriageBrief {
  summary: string;
  confidence: number;
  options: RankedOption[];
  walkStrategy?: WalkStrategy | null;
  executeLabel?: string | null;
}

// The human-approval gate record on an alert (backend ApprovalRecord).
export interface ApprovalRecord {
  state: ApprovalState;
  selectedOption?: string | null;
  decidedBy?: string | null;
  decidedAt?: string | null;
}

// Correlation pointer from an alert to the operational record (backend SourceEntityRef).
export interface SourceEntityRef {
  table?: string;
  propertyId?: string;
  entityKey?: string;
  ruleType?: string;
}

// ---------------------------------------------------------------------------
// Forecast (FORECAST_OVERSELL) models. These mirror the exact camelCase payload
// assembled by the backend Forecasting Agent's build_forecast_alert_item /
// _render_recommendation (forecast-agent/forecast/alerts.py). The backend stores
// the recommendation FLAT (narrative, steps, roomsOversold, overflowOption)
// rather than under a nested `plan`, so these interfaces follow that flat shape.
// ---------------------------------------------------------------------------

// A sister-property overflow candidate on a forecast recommendation. Present
// only when the plan carries a sister-property candidate (backend omits it
// otherwise).
export interface ForecastOverflowOption {
  sisterPropertyId: string;
  availableRooms: number;
}

// The advisory recommendation rendered from the backend RemediationPlan. Flat by
// design: the backend persists narrative/steps/roomsOversold directly on the
// recommendation, not under a `plan` sub-object.
export interface ForecastRecommendation {
  narrative: string;
  steps: string[];
  roomsOversold: number;
  overflowOption?: ForecastOverflowOption;
}

// The forecast sub-object on a FORECAST_OVERSELL alert (backend forecast_detail).
export interface ForecastDetail {
  conditionType: string;
  anticipatedDate: string;
  confidence: number;
  roomsOversold: number;
  basedOnPartialData: boolean;
  recommendation: ForecastRecommendation;
}

// A persisted alert record (the pulse-alerts item, backend Alert). Optional
// fields are absent until the corresponding lifecycle event occurs.
export interface Alert {
  alertId: string;
  propertyId: string;
  tier: AlertTier;
  type: AlertType;
  title: string;
  detail: string;
  status: AlertStatus;
  createdAt: string;
  dedupeKey: string;
  sourceEntityRef?: SourceEntityRef;
  gmAlias?: string | null;
  triageBrief?: TriageBrief | null;
  escalationStatus?: EscalationStatus;
  escalationReasons?: string[];
  escalationChain?: string[];
  escalationPosition?: number;
  escalationTimeoutMin?: number | null;
  escalationNextCheckAt?: string | null;
  incompleteInputData?: boolean;
  acknowledgedBy?: string | null;
  acknowledgedAt?: string | null;
  resolvedBy?: string | null;
  resolvedAt?: string | null;
  approval?: ApprovalRecord;
  lastStatusChangeAt?: string | null;
  // Predictive FORECAST_OVERSELL payload; present only on forecast alerts.
  forecast?: ForecastDetail;
  // Advisory-only marker: the backend sets applied=false at the alert top-level
  // (not inside forecast) since a forecast alert is never executed.
  applied?: boolean;
}

// Response envelope for GET /alerts (backend json_response body).
export interface AlertsListResponse {
  alerts: Alert[];
  count: number;
}

// Response envelope for GET /alerts/{alertId}.
export interface AlertDetailResponse {
  alert: Alert;
}

// Response envelope for POST /alerts/{alertId}/approvals.
export interface ApprovalResponse {
  accepted: boolean;
  approvalState: ApprovalState;
  selectedOption?: string | null;
  executed: boolean;
  execution?: Record<string, unknown>;
  reason?: string;
}

// Error envelope returned by the API on failures (backend error_response).
export interface ErrorResponse {
  error: {
    message: string;
    [key: string]: unknown;
  };
}


// ---------------------------------------------------------------------------
// VIPs tab models (GET /vips) - Task 21.2
// ---------------------------------------------------------------------------
//
// These mirror the exact JSON shaped by the pulse-ops-read facade
// (backend src/pulse/ops_read/vips.py). Guest profile fields are the camelCase
// keys the shared Gateway get_vip_guests tool returns; sensitiveNotes is already
// stripped server-side and is intentionally NOT modeled here (Requirement 15.10,
// 16.6).

// LUMI loyalty tiers, ordered by eliteness (AMBASSADOR > TITANIUM > PLATINUM).
// A string fallback is retained so an unexpected tier from the facade still
// renders without a type error.
export type VipTier = 'AMBASSADOR' | 'TITANIUM' | 'PLATINUM' | (string & {});

// A single VIP arrival profile (a guest entry within a tier group). Only guestId
// is guaranteed; every other field is optional so a sparse facade payload still
// renders.
export interface VipGuest {
  guestId: string;
  guestName?: string;
  initials?: string;
  loyaltyTier?: VipTier;
  loyaltyNumber?: string;
  totalStays?: number;
  roomNumber?: string;
  roomType?: string;
  estimatedArrival?: string;
  specialOccasion?: string | null;
  preferences?: string[];
  accountType?: string;
  corporateAccount?: string | null;
}

// A tier group: all VIP arrivals sharing one loyalty tier (facade groups these
// ordered by eliteness).
export interface VipTierGroup {
  tier: VipTier;
  count: number;
  guests: VipGuest[];
}

// Response envelope for GET /vips (backend shape_vips).
export interface VipsResponse {
  propertyId: string;
  date: string | null;
  vipCount: number;
  tiers: VipTierGroup[];
}

// ---------------------------------------------------------------------------
// Ops tab models (GET /ops) - Task 21.2
// ---------------------------------------------------------------------------
//
// Mirror the pulse-ops-read facade (backend src/pulse/ops_read/ops.py): a
// facility summary, OOO room cards each joined with their work-order status, and
// a group-checkout summary (Requirement 15.11).

// The facility summary KPIs on the Ops tab.
export interface OpsFacility {
  occupancyPct: number;
  arrivalsTotal: number;
  departuresTotal: number;
  confirmedReservations: number;
  availableRooms: number;
  oooCount: number;
  openWorkOrders: number;
}

// The work-order status joined onto an out-of-order room card. Null on the room
// card when the OOO room has no linked work order.
export interface WorkOrderSummary {
  workOrderId?: string;
  status?: string;
  priority?: string;
  issueType?: string;
  assignedTo?: string;
  createdAt?: string;
  estimatedResolutionHours?: number;
}

// A single out-of-order room card with its (optional) work-order status.
export interface OooRoom {
  roomNumber?: string;
  roomType?: string;
  status?: string;
  floor?: string | number;
  view?: string;
  isPremiumRoom?: boolean;
  workOrder: WorkOrderSummary | null;
}

// The group-checkout summary block on the Ops tab.
export interface GroupCheckout {
  departuresTotal: number;
  availableRooms: number;
  confirmedReservations: number;
}

// Response envelope for GET /ops (backend shape_ops).
export interface OpsResponse {
  propertyId: string;
  date: string | null;
  facility: OpsFacility;
  oooRooms: OooRoom[];
  groupCheckout: GroupCheckout;
}

// ---------------------------------------------------------------------------
// Kitchen tab models (GET /kitchen) - Task 21.2
// ---------------------------------------------------------------------------
//
// These mirror the exact JSON the PULSE REST API returns for the kitchen
// snapshot read. Field names are the camelCase DynamoDB attribute names stored
// in the pulse-kitchen table (one item per property); they are NOT invented
// here. The data was previously bundled in the PWA (lib/kitchenDemoData.ts) and
// is now served from a PULSE-owned table via the read-only API.

// SLA verdict for an in-flight order, driving its color treatment.
export type OrderSlaState = 'on-time' | 'at-risk' | 'breached';

// A tile in the F&B summary (orders, avg ticket, accuracy).
export interface FbStat {
  label: string;
  value: string;
  delta: string;
  // Whether the delta reads as positive (success) or cautionary (warning).
  deltaTone: 'success' | 'warning';
}

// A single in-flight order row in the live order feed.
export interface KitchenOrder {
  id: string;
  kind: 'room-service' | 'banquet' | 'fnb' | 'external';
  title: string;
  detail: string;
  elapsedLabel: string;
  slaState: OrderSlaState;
  slaLabel: string;
}

// A revenue-channel-mix slice.
export interface ChannelSlice {
  label: string;
  pct: number;
  // Highlight a slice that is off-target (e.g. third-party commission leakage).
  warning?: boolean;
}

// The active banquet countdown card.
export interface BanquetCountdown {
  title: string;
  badge: string;
  minutesRemaining: number;
  progressPct: number;
  subline: string;
}

// The delivery SLA tracker (room service).
export interface DeliverySla {
  label: string;
  pct: number;
  avgLabel: string;
  targetLabel: string;
  atRisk: number;
  standardLabel: string;
}

// Response envelope for GET /kitchen (backend _handle_kitchen). banquetCountdown
// and deliverySla are nullable so a sparse/unseeded snapshot still renders.
export interface KitchenResponse {
  propertyId: string;
  banquetCountdown: BanquetCountdown | null;
  fbStats: FbStat[];
  deliverySla: DeliverySla | null;
  kitchenOrders: KitchenOrder[];
  channelMix: ChannelSlice[];
  channelMixNote: string;
}

// ---------------------------------------------------------------------------
// Realtime models (AppSync Events) - Task 21.3
// ---------------------------------------------------------------------------

// The three event types the client acts on to drive the live feed, matching the
// backend delivery layer (src/pulse/delivery/realtime_publish.py).
export type RealtimeEventType = 'ALERT_CREATED' | 'ALERT_UPDATED' | 'ALERT_RESOLVED';

// The "full-enough" realtime event payload published to a pulse channel. It
// deliberately omits the heavy triageBrief; hasTriageBrief drives the
// agent-ready badge and the client fetches the full brief on demand. Field
// names match backend build_event exactly.
export interface RealtimeAlertEvent {
  eventType: RealtimeEventType;
  alertId: string;
  propertyId: string;
  tier?: AlertTier;
  type?: AlertType;
  status?: AlertStatus;
  title?: string;
  escalationStatus?: EscalationStatus;
  hasTriageBrief?: boolean;
  lastStatusChangeAt?: string | null;
}

// Response envelope for GET /config/realtime (backend _handle_realtime_config).
export interface RealtimeConfigResponse {
  httpEndpoint: string;
  wssEndpoint: string;
  namespace: string;
}

// Response envelope for GET /config/vapid-public-key (backend
// _handle_vapid_public_key).
export interface VapidPublicKeyResponse {
  publicKey: string;
}
