import { Check, Palette } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ACCENT_OPTIONS } from '@/lib/theme'
import { cn } from '@/lib/utils'
import { useSettingsStore } from '@/store/settingsStore'

export function AccentColorPicker() {
  const { t } = useTranslation()
  const accent = useSettingsStore((state) => state.accent)
  const setAccent = useSettingsStore((state) => state.setAccent)

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={t('sidebar.accentColor')}
          className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        >
          <Palette className="h-4 w-4" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end">
        <p className="mb-2 text-xs font-medium text-[var(--muted-foreground)]">
          {t('sidebar.accentColor')}
        </p>
        <div className="grid grid-cols-6 gap-2">
          {ACCENT_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              title={option.label}
              onClick={() => setAccent(option.id)}
              className={cn(
                'flex h-7 w-7 items-center justify-center rounded-full ring-offset-2 ring-offset-[var(--card)] transition-transform hover:scale-110 focus-visible:outline-none focus-visible:ring-2',
                accent === option.id && 'ring-2',
              )}
              style={{ backgroundColor: option.value }}
            >
              {accent === option.id && <Check className="h-3.5 w-3.5 text-white" />}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}
