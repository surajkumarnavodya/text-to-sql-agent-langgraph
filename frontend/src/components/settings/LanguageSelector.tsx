import { useTranslation } from 'react-i18next'
import { Select } from '@/components/ui/select'
import { SUPPORTED_LANGUAGES } from '@/i18n'
import { useSettingsStore } from '@/store/settingsStore'

export function LanguageSelector() {
  const { t, i18n } = useTranslation()
  const language = useSettingsStore((state) => state.language)
  const setLanguage = useSettingsStore((state) => state.setLanguage)

  const handleChange = (code: string) => {
    setLanguage(code)
    void i18n.changeLanguage(code)
  }

  return (
    <Select
      aria-label={t('sidebar.language')}
      value={language}
      onChange={(event) => handleChange(event.target.value)}
      className="w-full"
    >
      {SUPPORTED_LANGUAGES.map((lang) => (
        <option key={lang.code} value={lang.code}>
          {lang.label}
        </option>
      ))}
    </Select>
  )
}
