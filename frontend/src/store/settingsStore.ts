import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { applyAccent, applyFont, applyThemeMode, type ThemeMode } from '@/lib/theme'

interface SettingsState {
  themeMode: ThemeMode
  accent: string
  font: string
  language: string
  /** Sidebar section id -> collapsed. Missing id defaults to expanded. */
  collapsedSections: Record<string, boolean>
  /** Whether the whole desktop sidebar is hidden (a ChatGPT-style full
   * collapse toggled from the header) -- distinct from `collapsedSections`,
   * which only hides individual sections within an already-visible
   * sidebar, and from the mobile off-canvas drawer, which has its own
   * open/closed state local to AppShell since it's a transient overlay,
   * not a layout preference worth persisting. */
  sidebarCollapsed: boolean
  setThemeMode: (mode: ThemeMode) => void
  setAccent: (accentId: string) => void
  setFont: (fontId: string) => void
  setLanguage: (language: string) => void
  toggleSection: (sectionId: string) => void
  isSectionCollapsed: (sectionId: string) => boolean
  toggleSidebar: () => void
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
      sidebarCollapsed: false,
      toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
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
