import { resolveChartColors } from '@/lib/chartEngine'
import { useSettingsStore } from '@/store/settingsStore'

/** The live chart palette, re-resolved from the CSS variables whenever the
 * theme or accent changes -- a `<canvas>` can't reference `var(--x)` itself,
 * so the resolved colors are read again on every render. Subscribing to the
 * two settings is what makes that re-read happen on a theme/accent change
 * (the same pattern `ResultChart` uses for its own colors). */
export function useChartPalette(): string[] {
  useSettingsStore((state) => state.themeMode)
  useSettingsStore((state) => state.accent)
  return resolveChartColors()
}
