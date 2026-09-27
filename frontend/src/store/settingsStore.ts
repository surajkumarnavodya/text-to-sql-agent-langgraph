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
  /** Desktop (`lg:`+) left-rail collapse preference -- the single source of
   * truth for "is the persistent sidebar a full list or an icon rail."
   * Deliberately NOT the same state as MobileNav's own `open` (whether the
   * off-canvas drawer overlay is showing) -- that's ephemeral per-visit
   * interaction state, not a preference, and always starts closed on a
   * fresh page load regardless of this value. Persisted like
   * theme/accent/font (`sidebarCollapsed=true`/`false` in localStorage) --
   * a non-sensitive UI preference only, never chat content. */
  sidebarCollapsed: boolean
  /** The user's own Ollama model choice, from the "AI Model" picker in
   * Settings (HistorySettingsSection.tsx) -- a model id from the most
   * recent GET /models response, or null to use the server-configured
   * default. Persisted like theme/accent/font (a device preference). Unlike
   * that flag, this is validated server-side on every /ask (see
   * agent.model_registry.validate_model_selection) -- if a model an earlier
   * session picked is no longer in the server's allowed set (a config
   * change since then), HistorySettingsSection.tsx resets this back to
   * null the next time GET /models loads, rather than leaving every /ask
   * failing with a 400 until the user notices and re-picks manually. */
  selectedModel: string | null
  setThemeMode: (mode: ThemeMode) => void
  setAccent: (accentId: string) => void
  setFont: (fontId: string) => void
  setLanguage: (language: string) => void
  setVoiceModeEnabled: (enabled: boolean) => void
  setSelectedModel: (modelId: string | null) => void
  toggleSection: (sectionId: string) => void
  isSectionCollapsed: (sectionId: string) => boolean
  toggleSidebarCollapsed: () => void
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
      sidebarCollapsed: false,
      selectedModel: null,
      setVoiceModeEnabled: (enabled) => set({ voiceModeEnabled: enabled }),
      setSelectedModel: (modelId) => set({ selectedModel: modelId }),
      toggleSidebarCollapsed: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
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
