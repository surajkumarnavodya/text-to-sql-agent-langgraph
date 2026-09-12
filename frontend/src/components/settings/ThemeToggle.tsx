import { Monitor, Moon, Sun } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import type { ThemeMode } from '@/lib/theme'
import { useSettingsStore } from '@/store/settingsStore'

const OPTIONS: { mode: ThemeMode; icon: typeof Sun; key: 'light' | 'dark' | 'system' }[] = [
  { mode: 'light', icon: Sun, key: 'light' },
  { mode: 'dark', icon: Moon, key: 'dark' },
  { mode: 'system', icon: Monitor, key: 'system' },
]

export function ThemeToggle() {
  const { t } = useTranslation()
  const themeMode = useSettingsStore((state) => state.themeMode)
  const setThemeMode = useSettingsStore((state) => state.setThemeMode)

  return (
    <div
      role="radiogroup"
      aria-label={t('sidebar.theme')}
      className="inline-flex items-center gap-0.5 rounded-lg bg-[var(--muted)] p-1"
    >
      {OPTIONS.map(({ mode, icon: Icon, key }) => (
        <button
          key={mode}
          type="button"
          role="radio"
          aria-checked={themeMode === mode}
          title={t(`theme.${key}`)}
          onClick={() => setThemeMode(mode)}
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
            themeMode === mode
              ? 'bg-[var(--card)] text-[var(--accent)] shadow-sm'
              : 'text-[var(--muted-foreground)] hover:text-[var(--foreground)]',
          )}
        >
          <Icon className="h-4 w-4" />
        </button>
      ))}
    </div>
  )
}
