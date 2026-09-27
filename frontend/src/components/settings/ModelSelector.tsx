import { useEffect } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { useAvailableModels } from '@/hooks/queries'
import { useSettingsStore } from '@/store/settingsStore'

/** The "AI Model" picker -- lets the user choose which configured/allowed
 * Ollama model answers their Text-to-SQL questions, backed entirely by
 * `GET /models` (`agent.model_registry.build_model_options`). A native
 * `<select>` (see `components/ui/select.tsx`'s own docstring for why --
 * matches every other simple picker in this Settings panel: font, language,
 * document sensitivity). An option for a configured-but-not-installed model
 * is rendered `disabled` -- the browser itself refuses to let it be
 * selected, satisfying "unavailable models cannot be selected" without any
 * extra client-side validation logic.
 *
 * `AskRequest.model` (sent from `chatStore.ts`, read from
 * `useSettingsStore.selectedModel`) is still independently re-validated
 * server-side on every `/ask` regardless of what this component renders --
 * this UI is a convenience, never the security boundary (see
 * `agent.model_registry.validate_model_selection`).
 */
export function ModelSelector() {
  const { t } = useTranslation()
  const { data, isLoading, isError, refetch, isFetching } = useAvailableModels()
  const selectedModel = useSettingsStore((state) => state.selectedModel)
  const setSelectedModel = useSettingsStore((state) => state.setSelectedModel)

  // Reconciliation: a model this device picked in an earlier session may no
  // longer be in the server's own allowed set (an operator's .env change)
  // -- reset to "use the default" rather than leaving every /ask failing
  // with HTTP 400 until the user notices and re-opens this picker
  // themselves. Deliberately does NOT reset a merely-not-installed model
  // (still a valid, allowed choice -- the user may be mid-`ollama pull`).
  useEffect(() => {
    if (!data || selectedModel === null) return
    const stillAllowed = data.models.some((model) => model.id === selectedModel)
    if (!stillAllowed) setSelectedModel(null)
  }, [data, selectedModel, setSelectedModel])

  if (isError) {
    return <p className="text-xs text-[var(--muted-foreground)]">{t('sidebar.modelSelectionUnavailable')}</p>
  }
  if (isLoading || !data) {
    return null
  }
  // Selection hard-disabled (Settings.ollama_model_selection_enabled=false)
  // or, unusually, zero models configured -- the composer/graph still work
  // fine using the server default, this section just has nothing to show.
  if (!data.selection_enabled || data.models.length === 0) {
    return null
  }

  const effectiveValue = selectedModel ?? data.default_model
  const selectedOption = data.models.find((model) => model.id === effectiveValue)

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Select
          aria-label={t('sidebar.aiModel')}
          value={effectiveValue}
          onChange={(event) => {
            const value = event.target.value
            setSelectedModel(value === data.default_model ? null : value)
          }}
          className="flex-1"
        >
          {data.models.map((model) => (
            <option key={model.id} value={model.id} disabled={!model.installed}>
              {model.display_name}
              {model.is_default ? ` (${t('sidebar.modelDefault')})` : ''}
              {!model.installed ? ` — ${t('sidebar.modelNotInstalled')}` : ''}
            </option>
          ))}
        </Select>
        <Button
          size="sm"
          variant="secondary"
          aria-label={t('sidebar.modelRefresh')}
          onClick={() => void refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={isFetching ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
        </Button>
      </div>
      {selectedOption && (
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={selectedOption.installed ? 'success' : 'warning'}>
            {selectedOption.installed ? t('sidebar.modelAvailable') : t('sidebar.modelNotInstalled')}
          </Badge>
          {selectedOption.parameter_size && <Badge tone="neutral">{selectedOption.parameter_size}</Badge>}
          {!selectedOption.installed && (
            <span className="text-xs text-[var(--muted-foreground)]">
              {t('sidebar.modelPullHint', { model: selectedOption.id })}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
