import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Route, Routes } from 'react-router-dom'
import { AuthGate } from '@/components/auth/AuthGate'
import { AppShell } from '@/components/layout/AppShell'
import { PwaUpdateBanner } from '@/components/layout/PwaUpdateBanner'
import { ToastProvider } from '@/components/ui/toast'
import { watchSystemTheme, applyThemeMode } from '@/lib/theme'
import { AuthCallback } from '@/pages/AuthCallback'
import { Chat } from '@/pages/Chat'
import { KnowledgeSources } from '@/pages/KnowledgeSources'
import { MediaSearch } from '@/pages/MediaSearch'
import { useSettingsStore } from '@/store/settingsStore'

export function App() {
  const { i18n } = useTranslation()
  const language = useSettingsStore((state) => state.language)
  const themeMode = useSettingsStore((state) => state.themeMode)

  // Sync the persisted language preference into i18next once on mount
  // (and whenever it changes elsewhere, e.g. a future settings-sync), and
  // keep <html lang> in step with it -- screen readers use it to pick the
  // right pronunciation/hyphenation rules, and it's a correctness detail
  // that's easy to forget once a language switcher exists at all.
  useEffect(() => {
    void i18n.changeLanguage(language)
    document.documentElement.lang = language
  }, [i18n, language])

  // Re-resolve 'system' theme mode when the OS preference flips mid-session.
  useEffect(() => {
    if (themeMode !== 'system') return
    return watchSystemTheme(() => applyThemeMode('system'))
  }, [themeMode])

  return (
    <ToastProvider>
      <PwaUpdateBanner />
      <Routes>
        {/* Reachable without being signed in -- this is where the identity
            provider sends the user back to after a successful login, so it
            must never itself sit behind AuthGate. */}
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route
          element={
            <AuthGate>
              <AppShell />
            </AuthGate>
          }
        >
          <Route path="/" element={<Chat />} />
          <Route path="/knowledge-sources" element={<KnowledgeSources />} />
          <Route path="/media-search" element={<MediaSearch />} />
        </Route>
      </Routes>
    </ToastProvider>
  )
}
