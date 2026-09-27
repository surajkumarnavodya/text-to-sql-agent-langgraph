import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import type { ModelsResponse } from '@/lib/types'
import { useSettingsStore } from '@/store/settingsStore'
import { ModelSelector } from './ModelSelector'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, getAvailableModels: vi.fn() }
})

const initialSettingsState = useSettingsStore.getState()

function renderSelector() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ModelSelector />
    </QueryClientProvider>,
  )
}

function response(overrides: Partial<ModelsResponse> = {}): ModelsResponse {
  return {
    provider: 'ollama',
    default_model: 'llama3.1:8b',
    selection_enabled: true,
    models: [
      {
        id: 'llama3.1:8b',
        display_name: 'Llama 3.1 8B',
        is_default: true,
        enabled: true,
        installed: true,
        available: true,
        recommended: true,
        parameter_size: '8B',
        context_length: 128000,
        resource_level: 'medium',
        capabilities: ['text', 'sql'],
        description: '',
      },
      {
        id: 'qwen2.5:7b',
        display_name: 'Qwen 2.5 7B',
        is_default: false,
        enabled: true,
        installed: false,
        available: false,
        recommended: true,
        parameter_size: '7B',
        context_length: 32000,
        resource_level: 'medium',
        capabilities: ['text', 'sql'],
        description: '',
      },
    ],
    ...overrides,
  }
}

describe('ModelSelector', () => {
  beforeEach(() => {
    useSettingsStore.setState(initialSettingsState, true)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    useSettingsStore.setState(initialSettingsState, true)
  })

  it('renders every configured model as an option', async () => {
    vi.mocked(api.getAvailableModels).mockResolvedValue(response())

    renderSelector()

    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument())
    expect(screen.getByRole('option', { name: /Llama 3.1 8B/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Qwen 2.5 7B/ })).toBeInTheDocument()
  })

  it('disables an option for a model that is not installed', async () => {
    vi.mocked(api.getAvailableModels).mockResolvedValue(response())

    renderSelector()

    const uninstalled = await screen.findByRole('option', { name: /Qwen 2.5 7B/ })
    expect(uninstalled).toBeDisabled()
    const installed = screen.getByRole('option', { name: /Llama 3.1 8B/ })
    expect(installed).not.toBeDisabled()
  })

  it('selecting a model updates the settings store', async () => {
    vi.mocked(api.getAvailableModels).mockResolvedValue(
      response({
        models: [
          {
            id: 'llama3.1:8b',
            display_name: 'Llama 3.1 8B',
            is_default: true,
            enabled: true,
            installed: true,
            available: true,
            recommended: true,
            parameter_size: '8B',
            context_length: 128000,
            resource_level: 'medium',
            capabilities: ['text', 'sql'],
            description: '',
          },
          {
            id: 'mistral:7b',
            display_name: 'Mistral 7B',
            is_default: false,
            enabled: true,
            installed: true,
            available: true,
            recommended: false,
            parameter_size: '7B',
            context_length: 32000,
            resource_level: 'medium',
            capabilities: ['text', 'sql'],
            description: '',
          },
        ],
      }),
    )

    renderSelector()
    await screen.findByRole('option', { name: /Mistral 7B/ })

    await userEvent.selectOptions(screen.getByRole('combobox'), 'mistral:7b')

    expect(useSettingsStore.getState().selectedModel).toBe('mistral:7b')
  })

  it('selecting the default model back stores null, not the literal default id', async () => {
    useSettingsStore.setState({ selectedModel: 'qwen2.5:7b' })
    vi.mocked(api.getAvailableModels).mockResolvedValue(
      response({
        models: [
          {
            id: 'llama3.1:8b',
            display_name: 'Llama 3.1 8B',
            is_default: true,
            enabled: true,
            installed: true,
            available: true,
            recommended: true,
            parameter_size: '8B',
            context_length: 128000,
            resource_level: 'medium',
            capabilities: ['text', 'sql'],
            description: '',
          },
          {
            id: 'qwen2.5:7b',
            display_name: 'Qwen 2.5 7B',
            is_default: false,
            enabled: true,
            installed: true,
            available: true,
            recommended: true,
            parameter_size: '7B',
            context_length: 32000,
            resource_level: 'medium',
            capabilities: ['text', 'sql'],
            description: '',
          },
        ],
      }),
    )

    renderSelector()
    await screen.findByRole('option', { name: /Qwen 2.5 7B/ })

    await userEvent.selectOptions(screen.getByRole('combobox'), 'llama3.1:8b')

    expect(useSettingsStore.getState().selectedModel).toBeNull()
  })

  it('resets a persisted selection that is no longer in the allowed set', async () => {
    useSettingsStore.setState({ selectedModel: 'removed-model:1b' })
    vi.mocked(api.getAvailableModels).mockResolvedValue(response())

    renderSelector()

    await waitFor(() => expect(useSettingsStore.getState().selectedModel).toBeNull())
  })

  it('does not reset a persisted selection that is merely not installed yet', async () => {
    useSettingsStore.setState({ selectedModel: 'qwen2.5:7b' })
    vi.mocked(api.getAvailableModels).mockResolvedValue(response())

    renderSelector()
    await screen.findByRole('combobox')

    expect(useSettingsStore.getState().selectedModel).toBe('qwen2.5:7b')
  })

  it('renders nothing when model selection is disabled server-side', async () => {
    vi.mocked(api.getAvailableModels).mockResolvedValue(
      response({ selection_enabled: false, models: [response().models[0]] }),
    )

    const { container } = renderSelector()

    await waitFor(() => expect(api.getAvailableModels).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a fallback message, not a crash, when the models request fails', async () => {
    vi.mocked(api.getAvailableModels).mockRejectedValue(new Error('network down'))

    renderSelector()

    await waitFor(() => expect(screen.getByText(/model list unavailable/i)).toBeInTheDocument())
  })
})
