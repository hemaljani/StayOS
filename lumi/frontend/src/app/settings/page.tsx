'use client';

import { useSettings } from '@/hooks/useSettings';
import { useAuth } from '@/hooks/useAuth';
import SettingsForm from '@/components/SettingsForm';
import { LogOut } from 'lucide-react';

export default function SettingsPage() {
  const { settings, loading, error, saving, saveError, saveSuccess, updateSettings } = useSettings();
  const { logout } = useAuth();

  if (loading) {
    return <div className="py-8 text-center text-gray-400">Loading settings...</div>;
  }

  if (error || !settings) {
    return <div className="py-8 text-center text-danger">{error || 'Failed to load settings'}</div>;
  }

  return (
    <div className="py-4">
      {/* GM greeting (matches Brief page) */}
      {settings.gmName && (
        <div className="mb-4">
          <h1 className="text-xl font-semibold">Hello, {settings.gmName.split(' ')[0]}</h1>
          {settings.propertyName && (
            <p className="text-sm text-gray-400">{settings.propertyName}</p>
          )}
        </div>
      )}
      <h2 className="text-lg font-semibold mb-4">Settings</h2>
      <SettingsForm
        settings={settings}
        saving={saving}
        saveError={saveError}
        saveSuccess={saveSuccess}
        onSave={updateSettings}
      />

      {/* Logout */}
      <button
        type="button"
        onClick={logout}
        className="w-full mt-6 flex items-center justify-center gap-2 bg-surface border border-gray-700 text-danger font-medium py-3 rounded-xl hover:bg-danger/10 transition-colors"
      >
        <LogOut size={18} />
        Sign Out
      </button>
    </div>
  );
}
