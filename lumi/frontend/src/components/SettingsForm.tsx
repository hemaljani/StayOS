'use client';

import { useState } from 'react';
import type { GmSettings, AlertToggles, KpiThresholds, AudioPreferences } from '@/lib/types';

interface SettingsFormProps {
  settings: GmSettings;
  saving: boolean;
  saveError: string | null;
  saveSuccess: boolean;
  onSave: (updates: Partial<GmSettings>) => void;
}

const LANGUAGE_OPTIONS = [
  { value: 'en-US', label: 'English (US)' },
  { value: 'es-ES', label: 'Spanish (Spain)' },
  { value: 'ja-JP', label: 'Japanese' },
  { value: 'zh-CN', label: 'Mandarin (China)' },
] as const;

const BRIEF_LENGTH_OPTIONS = [
  { value: 'brief', label: 'Brief (~60s)' },
  { value: 'standard', label: 'Standard (~90s)' },
  { value: 'detailed', label: 'Detailed (~120s)' },
] as const;

export default function SettingsForm({
  settings,
  saving,
  saveError,
  saveSuccess,
  onSave,
}: SettingsFormProps) {
  const [deliveryTime, setDeliveryTime] = useState(settings.briefDeliveryTime);
  const [toggles, setToggles] = useState<AlertToggles>(settings.alertToggles);
  const [thresholds, setThresholds] = useState<KpiThresholds>(settings.kpiThresholds);
  const [audioPrefs, setAudioPrefs] = useState<AudioPreferences>(settings.audioPreferences);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const validate = (): boolean => {
    const newErrors: Record<string, string> = {};

    // Validate delivery time format HH:MM
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(deliveryTime)) {
      newErrors.deliveryTime = 'Use HH:MM format (00:00 - 23:59)';
    }

    // Validate occupancy threshold 0-100
    if (thresholds.occupancyAlertBelow < 0 || thresholds.occupancyAlertBelow > 100) {
      newErrors.occupancy = 'Must be between 0 and 100';
    }

    // Validate ADR threshold 0-1000
    if (thresholds.adrAlertBelow < 0 || thresholds.adrAlertBelow > 1000) {
      newErrors.adr = 'Must be between 0 and 1000';
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;

    onSave({
      briefDeliveryTime: deliveryTime,
      alertToggles: toggles,
      kpiThresholds: thresholds,
      audioPreferences: audioPrefs,
    });
  };

  const handleToggle = (key: keyof AlertToggles) => {
    setToggles((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      {/* Delivery time */}
      <div className="bg-surface rounded-xl p-4">
        <h3 className="text-xs text-gray-500 uppercase tracking-wide mb-2">Brief Delivery Time</h3>
        <input
          type="time"
          value={deliveryTime}
          onChange={(e) => setDeliveryTime(e.target.value)}
          className="w-full bg-background text-ink rounded-lg px-3 py-2 text-sm border border-gray-700 focus:border-accent focus:outline-none"
        />
        {errors.deliveryTime && (
          <p className="text-xs text-danger mt-1">{errors.deliveryTime}</p>
        )}
      </div>

      {/* Alert toggles */}
      <div className="bg-surface rounded-xl p-4">
        <h3 className="text-xs text-gray-500 uppercase tracking-wide mb-3">Alert Preferences</h3>
        <div className="space-y-3">
          <ToggleRow
            label="Overbooking Risk"
            checked={toggles.overbookingRisk}
            onChange={() => handleToggle('overbookingRisk')}
          />
          <ToggleRow
            label="Rooms Out of Order"
            checked={toggles.roomsOutOfOrder}
            onChange={() => handleToggle('roomsOutOfOrder')}
          />
          <ToggleRow
            label="VIP Arrival Alert"
            checked={toggles.vipArrivalAlert}
            onChange={() => handleToggle('vipArrivalAlert')}
          />
          <ToggleRow
            label="Upsell Opportunity"
            checked={toggles.upsellOpportunity}
            onChange={() => handleToggle('upsellOpportunity')}
          />
          <ToggleRow
            label="Staffing Confirmed"
            checked={toggles.staffingConfirmed}
            onChange={() => handleToggle('staffingConfirmed')}
          />
        </div>
      </div>

      {/* KPI Thresholds */}
      <div className="bg-surface rounded-xl p-4">
        <h3 className="text-xs text-gray-500 uppercase tracking-wide mb-3">KPI Thresholds</h3>
        <div className="space-y-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">
              Occupancy alert below (%)
            </label>
            <input
              type="number"
              min={0}
              max={100}
              value={thresholds.occupancyAlertBelow}
              onChange={(e) => setThresholds((prev) => ({ ...prev, occupancyAlertBelow: Number(e.target.value) }))}
              className="w-full bg-background text-ink rounded-lg px-3 py-2 text-sm border border-gray-700 focus:border-accent focus:outline-none"
            />
            {errors.occupancy && (
              <p className="text-xs text-danger mt-1">{errors.occupancy}</p>
            )}
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">
              ADR alert below ($)
            </label>
            <input
              type="number"
              min={0}
              max={1000}
              value={thresholds.adrAlertBelow}
              onChange={(e) => setThresholds((prev) => ({ ...prev, adrAlertBelow: Number(e.target.value) }))}
              className="w-full bg-background text-ink rounded-lg px-3 py-2 text-sm border border-gray-700 focus:border-accent focus:outline-none"
            />
            {errors.adr && (
              <p className="text-xs text-danger mt-1">{errors.adr}</p>
            )}
          </div>
        </div>
      </div>

      {/* Audio Language */}
      <div className="bg-surface rounded-xl p-4">
        <h3 className="text-xs text-gray-500 uppercase tracking-wide mb-3">Audio Preferences</h3>
        <div className="mb-3">
          <label className="text-xs text-gray-400 block mb-1">Language</label>
          <select
            value={audioPrefs.language}
            onChange={(e) => setAudioPrefs((prev) => ({ ...prev, language: e.target.value as AudioPreferences['language'] }))}
            className="w-full bg-background text-ink rounded-lg px-3 py-2 text-sm border border-gray-700 focus:border-accent focus:outline-none"
          >
            {LANGUAGE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        {/* Brief length radio group */}
        <div>
          <label className="text-xs text-gray-400 block mb-2">Brief Length</label>
          <div className="flex gap-2">
            {BRIEF_LENGTH_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setAudioPrefs((prev) => ({ ...prev, briefLength: opt.value }))}
                className={`flex-1 text-xs py-2 rounded-lg border transition-colors ${
                  audioPrefs.briefLength === opt.value
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-gray-700 text-gray-400'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Save button */}
      <button
        type="submit"
        disabled={saving}
        className="w-full bg-accent text-white font-medium py-3 rounded-xl disabled:opacity-50 active:scale-[0.98] transition-transform"
      >
        {saving ? 'Saving...' : 'Save Settings'}
      </button>

      {/* Feedback messages */}
      {saveSuccess && (
        <p className="text-center text-sm text-success">Settings saved successfully!</p>
      )}
      {saveError && (
        <p className="text-center text-sm text-danger">{saveError}</p>
      )}
    </form>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-gray-300">{label}</span>
      <button
        type="button"
        onClick={onChange}
        className={`w-10 h-5 rounded-full transition-colors relative ${
          checked ? 'bg-accent' : 'bg-gray-600'
        }`}
        role="switch"
        aria-checked={checked}
        aria-label={label}
      >
        <span
          className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
            checked ? 'translate-x-5' : 'translate-x-0.5'
          }`}
        />
      </button>
    </div>
  );
}
