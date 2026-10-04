import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@/components/ui/toast'
import * as api from '@/lib/api'
import type { AskResponse, HealthResponse } from '@/lib/types'
import { useChatStore } from '@/store/chatStore'
import { ChatInput } from './ChatInput'

function makeHealth(): HealthResponse {
  return {
    status: 'ok',
    databases: [],
    ollama: { status: 'ok', detail: null } as unknown as HealthResponse['ollama'],
    voice_enabled: false,
    media_search_enabled: false,
    local_auth_enabled: false,
    google_signin_enabled: false,
    google_client_id: null,
    vision_enabled: false,
    vision_provider: null,
    vision_model: null,
    vision_model_available: null,
    ocr_enabled: false,
  }
}

function makeAskResponse(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    session_id: 's1',
    conversation_id: null,
    message_id: null,
    status: 'succeeded',
    database: 'default',
    model: 'llama3.1:8b',
    sql: null,
    result_columns: null,
    result_rows: null,
    row_count: null,
    retry_count: 0,
    attempt_history: [],
    insight: null,
    cost_notice: null,
    low_confidence_notice: null,
    rejection_reason: null,
    rejection_message: null,
    rate_limit_message: null,
    clarification_message: null,
    failure_explanation: null,
    error_history: [],
    sources_used: ['attachments'],
    synthesized_answer: 'Here is what the file says.',
    document_result: null,
    policy_result: null,
    web_result: null,
    generation_result: null,
    media_search_result: null,
    attachment_result: null,
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
    permission_denied_notice: null,
    analytical_result: null,
    forecast_result: null,
    recommendations: [],
    analytical_intent: null,
    analytical_plan: null,
    governing_metrics: [],
    restricted_field_notice: null,
    ...overrides,
  }
}

function makeTextFile(name = 'notes.txt'): File {
  return new File(['hello world'], name, { type: 'text/plain' })
}

function renderChatInput() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ChatInput />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

async function attachAndUpload(filename: string) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  await userEvent.upload(input, makeTextFile(filename))
  await waitFor(() => expect(screen.getByText(filename)).toBeInTheDocument())
  // Wait for the chip to leave its "uploading" state before the composer is
  // allowed to submit (ChatInput.submit() blocks while attachments.isUploading).
  await waitFor(() => expect(screen.queryByText(/uploading/i)).not.toBeInTheDocument())
}

const initialChatState = useChatStore.getState()

describe('ChatInput -- attachment composer lifecycle', () => {
  beforeEach(() => {
    useChatStore.setState(initialChatState, true)
    vi.spyOn(api, 'getHealth').mockResolvedValue(makeHealth())
    vi.spyOn(api, 'uploadAttachments').mockResolvedValue({
      attachments: [
        {
          attachment_id: 'att_1',
          filename: 'notes.txt',
          media_type: 'text/plain',
          size_bytes: 11,
          processing_status: 'succeeded',
          processing_error: null,
        },
      ],
      errors: [],
    })
    vi.spyOn(api, 'deleteAttachment').mockResolvedValue(undefined)
  })

  afterEach(() => {
    useChatStore.setState(initialChatState, true)
    vi.restoreAllMocks()
  })

  it('clears the pending attachment chip automatically once the backend accepts and answers the request', async () => {
    vi.spyOn(api, 'askQuestion').mockResolvedValue(makeAskResponse())
    const user = userEvent.setup()
    renderChatInput()

    await attachAndUpload('notes.txt')
    expect(screen.getByText('notes.txt')).toBeInTheDocument()

    await user.type(screen.getByLabelText(/ask a question/i), 'What does this file say?')
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.queryByText('notes.txt')).not.toBeInTheDocument())

    // The actual server-assigned attachment id was sent, not a blob URL or
    // bare filename -- the composer wouldn't have anything else to send.
    expect(api.askQuestion).toHaveBeenCalledWith(
      expect.objectContaining({ attachment_ids: ['att_1'] }),
      expect.anything(),
    )
  })

  it('clears the composer even when the agent itself fails to answer -- the request still reached the backend', async () => {
    vi.spyOn(api, 'askQuestion').mockResolvedValue(
      makeAskResponse({ status: 'failed', failure_explanation: 'The request failed.' }),
    )
    const user = userEvent.setup()
    renderChatInput()

    await attachAndUpload('notes.txt')
    await user.type(screen.getByLabelText(/ask a question/i), 'Describe it')
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.queryByText('notes.txt')).not.toBeInTheDocument())
  })

  it('preserves the attachment for retry when the request never reaches the backend', async () => {
    vi.spyOn(api, 'askQuestion').mockRejectedValue(new Error('network down'))
    const user = userEvent.setup()
    renderChatInput()

    await attachAndUpload('notes.txt')
    await user.type(screen.getByLabelText(/ask a question/i), 'Describe it')
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(api.askQuestion).toHaveBeenCalled())
    // Still there -- a client-side/network failure never reached the
    // backend, so the user shouldn't have to re-select the file to retry.
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
  })

  it('clears the composer only once, even though the same file could otherwise still be resolved after clearing', async () => {
    vi.spyOn(api, 'askQuestion').mockResolvedValue(makeAskResponse())
    const user = userEvent.setup()
    renderChatInput()

    await attachAndUpload('notes.txt')
    await user.type(screen.getByLabelText(/ask a question/i), 'Describe it')
    await user.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => expect(screen.queryByText('notes.txt')).not.toBeInTheDocument())
    expect(api.uploadAttachments).toHaveBeenCalledTimes(1)
    expect(api.askQuestion).toHaveBeenCalledTimes(1)
  })
})

describe('ChatInput -- composer layout', () => {
  it('shows the AI disclaimer below the composer', () => {
    renderChatInput()
    expect(screen.getByText('AI may make mistakes. Please verify results before relying on them.')).toBeInTheDocument()
  })

  it('starts as a single-line textarea', () => {
    renderChatInput()
    const textarea = screen.getByRole('textbox', { name: /ask|question|placeholder/i }) as HTMLTextAreaElement
    expect(textarea.rows).toBe(1)
  })
})

describe('ChatInput -- typing while an answer is pending', () => {
  afterEach(() => {
    useChatStore.setState({ pendingQuestion: null } as never)
  })

  it('keeps the textbox editable and offers stop instead of send while the agent works', async () => {
    useChatStore.setState({ pendingQuestion: { question: 'first', startedAt: Date.now() } } as never)
    renderChatInput()

    const textarea = screen.getByRole('textbox', { name: /chat|ask|placeholder/i }) as HTMLTextAreaElement
    expect(textarea).toBeEnabled()
    expect(textarea).not.toHaveAttribute('readonly')
    await userEvent.type(textarea, 'next question')
    expect(textarea.value).toBe('next question')

    expect(screen.queryByRole('button', { name: /^send$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /stop/i })).toBeInTheDocument()
  })
})
