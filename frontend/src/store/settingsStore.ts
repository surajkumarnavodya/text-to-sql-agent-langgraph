import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { applyAccent, applyFont, applyThemeMode, type ThemeMode } from '@/lib/theme'

interface SettingsState {
  themeMode: ThemeMode
  accent: string
  font: string
  language: string
  /** Settings-section id -> collapsed. Missing id defaults to expanded. */
  collapsedSections: Record<string, boolean>
  /** User's own opt-in, independent of the server's `voice_enabled` infra
   * flag (`GET /health`) -- both must be true for the mic button/settings
   * row to actually appear. Defaults to true (voice mode costs nothing and
   * calls no external API, unlike media generation's approval gate) --
   * the user can still turn it off per-device via the settings toggle.
   * Persisted like theme/accent/font (a device preference), not
   * session-only like `chatStore`'s `enableInsight`. */
  voiceModeEnabled: boolean
  setThemeMode: (mode: ThemeMode) => void
  setAccent: (accentId: string) => void
  setFont: (fontId: string) => void
  setLanguage: (language: string) => void
  setVoiceModeEnabled: (enabled: boolean) => void
  toggleSection: (sectionId: string) => void
  isSectionCollapsed: (sectionId: string) => boolean
}

/** Applies the persisted (or default) appearance to the DOM immediately --
 * called once at module load, before React even renders, so there's no
 * flash of the wrong theme/accent/font on refresh. */
function applyAppearance(state: Pick<SettingsState, 'themeMode' | 'accent' | 'font'>): void {
  applyThemeMode(state.themeMode)
  applyAccent(state.accent)
  applyFont(state.font)
}

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set, get) => ({
      themeMode: 'light',
      accent: 'indigo',
      font: 'system',
      language: 'en',
      collapsedSections: {},
      voiceModeEnabled: true,
      setVoiceModeEnabled: (enabled) => set({ voiceModeEnabled: enabled }),
      setThemeMode: (mode) => {
        applyThemeMode(mode)
        set({ themeMode: mode })
      },
      setAccent: (accentId) => {
        applyAccent(accentId)
        set({ accent: accentId })
      },
      setFont: (fontId) => {
        applyFont(fontId)
        set({ font: fontId })
      },
      setLanguage: (language) => set({ language }),
      toggleSection: (sectionId) =>
        set((state) => ({
          collapsedSections: {
            ...state.collapsedSections,
            [sectionId]: !state.collapsedSections[sectionId],
          },
        })),
      isSectionCollapsed: (sectionId) => Boolean(get().collapsedSections[sectionId]),
    }),
    {
      name: 'tsql-settings',
      onRehydrateStorage: () => (state) => {
        if (state) applyAppearance(state)
      },
    },
  ),
)

// Applies the default appearance immediately for a first-ever visit
// (before persisted state, if any, has finished rehydrating).
applyAppearance(useSettingsStore.getState())
