import { useTranslation } from 'react-i18next'
import { useRegisterSW } from 'virtual:pwa-register/react'
import { Button } from '@/components/ui/button'

/** Surfaces a "new version available" banner when the service worker has
 * fetched an update -- `registerType: 'autoUpdate'` (vite.config.ts) would
 * otherwise swap it in silently on the *next* full reload; this gives the
 * user an explicit, immediate way to pick that reload up now instead. */
export function PwaUpdateBanner() {
  const { t } = useTranslation()
  const { needRefresh, updateServiceWorker } = useRegisterSW()
  const [needsRefresh] = needRefresh

  if (!needsRefresh) return null

  return (
    <div className="flex items-center justify-center gap-3 bg-[var(--accent)] px-4 py-2 text-sm text-[var(--accent-foreground)]">
      <span>{t('sidebar.updateAvailable')}</span>
      <Button size="sm" variant="secondary" onClick={() => void updateServiceWorker(true)}>
        {t('sidebar.reload')}
      </Button>
    </div>
  )
}
