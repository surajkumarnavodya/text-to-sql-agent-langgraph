import i18n from 'i18next'
import LanguageDetector from 'i18next-browser-languagedetector'
import { initReactI18next } from 'react-i18next'
import en from './locales/en/translation.json'
import es from './locales/es/translation.json'
import fr from './locales/fr/translation.json'
import hi from './locales/hi/translation.json'
import mr from './locales/mr/translation.json'

export const SUPPORTED_LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Español' },
  { code: 'fr', label: 'Français' },
  { code: 'hi', label: 'हिन्दी' },
  { code: 'mr', label: 'मराठी' },
] as const

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      es: { translation: es },
      fr: { translation: fr },
      hi: { translation: hi },
      mr: { translation: mr },
    },
    fallbackLng: 'en',
    interpolation: { escapeValue: false },
    detection: {
      // useSettingsStore's persisted `language` is the source of truth once
      // the user has picked one explicitly (App.tsx calls i18n.changeLanguage
      // from it on mount) -- browser/localStorage detection here only
      // supplies the very first, pre-choice default.
      order: ['localStorage', 'navigator'],
      caches: [],
    },
  })

export default i18n
