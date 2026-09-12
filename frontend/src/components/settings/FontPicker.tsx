import { useTranslation } from 'react-i18next'
import { Select } from '@/components/ui/select'
import { FONT_OPTIONS } from '@/lib/theme'
import { useSettingsStore } from '@/store/settingsStore'

export function FontPicker() {
  const { t } = useTranslation()
  const font = useSettingsStore((state) => state.font)
  const setFont = useSettingsStore((state) => state.setFont)

  return (
    <Select
      aria-label={t('sidebar.font')}
      value={font}
      onChange={(event) => setFont(event.target.value)}
      className="w-full"
    >
      {FONT_OPTIONS.map((option) => (
        <option key={option.id} value={option.id}>
          {option.label}
        </option>
      ))}
    </Select>
  )
}
