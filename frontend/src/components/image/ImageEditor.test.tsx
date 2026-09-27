import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import type { AttachmentCapabilities } from '@/lib/types'
import { ImageEditor } from './ImageEditor'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    getAttachmentCapabilities: vi.fn(),
    uploadAttachments: vi.fn(),
    deleteAttachment: vi.fn(),
    aiEditAttachmentImage: vi.fn(),
    blurAttachmentRegion: vi.fn(),
    extractAttachmentText: vi.fn(),
  }
})

function capabilities(overrides: Partial<AttachmentCapabilities> = {}): AttachmentCapabilities {
  return {
    enabled: true,
    vision_input: true,
    vision_model: 'llava',
    ocr: true,
    image_resize: true,
    image_blur: true,
    image_text_removal: true,
    image_text_removal_method: 'inpaint',
    image_ai_editing: true,
    image_ai_editing_provider: 'ima_studio',
    native_pdf_input: false,
    max_image_bytes: 1_000_000,
    max_document_bytes: 1_000_000,
    max_attachments_per_message: 5,
    max_total_attachment_bytes: 5_000_000,
    max_resize_dimension_px: 4096,
    max_text_removal_regions: 10,
    max_ai_edit_prompt_length: 500,
    supported_image_extensions: ['png'],
    supported_document_extensions: ['pdf'],
    resize_presets: [],
    ...overrides,
  }
}

function renderEditor(props: Partial<Parameters<typeof ImageEditor>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSave = vi.fn()
  const onOpenChange = vi.fn()
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <ImageEditor
        open
        onOpenChange={onOpenChange}
        imageSrc="data:image/png;base64,AAAA"
        fileName="photo.png"
        onSave={onSave}
        {...props}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onSave, onOpenChange }
}

describe('ImageEditor -- AI-guided editing capability gating', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('disables the AI-edit prompt/Generate button and shows the not-configured notice when the server capability is off', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities({ image_ai_editing: false }))
    renderEditor()

    await waitFor(() =>
      expect(screen.getByText(/AI-guided editing is not configured on this server/i)).toBeInTheDocument(),
    )
    expect(screen.getByPlaceholderText(/e\.g\. remove the selected object/i)).toBeDisabled()
    expect(screen.getByRole('button', { name: /^generate$/i })).toBeDisabled()
    // Preset buttons for the paid AI operations are disabled too.
    expect(screen.getByRole('button', { name: 'Remove the selected object' })).toBeDisabled()
  })

  it('enables the AI-edit prompt/Generate button once the server reports the capability as available', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities({ image_ai_editing: true }))
    renderEditor()

    await waitFor(() => expect(screen.getByPlaceholderText(/e\.g\. remove the selected object/i)).not.toBeDisabled())
    expect(screen.queryByText(/AI-guided editing is not configured on this server/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove the selected object' })).not.toBeDisabled()
  })

  it('a preset button prefills the prompt with its own instruction text', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities())
    const user = userEvent.setup()
    renderEditor()

    const presetButton = await screen.findByRole('button', { name: 'Replace the sky with a sunset' })
    await user.click(presetButton)

    expect(screen.getByPlaceholderText(/e\.g\. remove the selected object/i)).toHaveValue(
      'Replace the sky with a sunset',
    )
  })

  it('still lets local blur/extract work when AI-guided editing is unavailable', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities({ image_ai_editing: false }))
    renderEditor()

    await waitFor(() =>
      expect(screen.getByText(/AI-guided editing is not configured on this server/i)).toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'Blur the selected face' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Extract the selected table or chart' })).not.toBeDisabled()
  })
})

describe('ImageEditor -- quick-action routing (blur/extract never reach the paid AI-edit endpoint)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('"Blur the selected face" without a painted mask shows an actionable error and calls no endpoint at all', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities())
    const user = userEvent.setup()
    renderEditor()

    const blurButton = await screen.findByRole('button', { name: 'Blur the selected face' })
    await user.click(blurButton)

    await waitFor(() =>
      expect(screen.getByText(/paint over the region to blur with the mask tool first/i)).toBeInTheDocument(),
    )
    expect(api.uploadAttachments).not.toHaveBeenCalled()
    expect(api.blurAttachmentRegion).not.toHaveBeenCalled()
    expect(api.aiEditAttachmentImage).not.toHaveBeenCalled()
  })

  it('"Extract the selected table or chart" calls OCR analysis only -- never the AI-edit endpoint, never a generated image', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities())
    vi.mocked(api.uploadAttachments).mockResolvedValue({
      attachments: [
        {
          attachment_id: 'scratch-1',
          filename: 'source.png',
          media_type: 'image/png',
          size_bytes: 10,
          processing_status: 'ready',
          processing_error: null,
        },
      ],
      errors: [],
    })
    vi.mocked(api.extractAttachmentText).mockResolvedValue({
      attachment_id: 'scratch-1',
      operation: 'extract_text',
      raw_text: 'Revenue | Q1 | Q2\n100 | 200',
      cleaned_text: 'Revenue | Q1 | Q2\n100 | 200',
      regions: [],
      warnings: [],
    })
    vi.mocked(api.deleteAttachment).mockResolvedValue(undefined)
    const user = userEvent.setup()
    renderEditor()

    const extractButton = await screen.findByRole('button', { name: 'Extract the selected table or chart' })
    await user.click(extractButton)

    await waitFor(() => expect(api.extractAttachmentText).toHaveBeenCalledWith('scratch-1'))
    await waitFor(() => expect(screen.getByText(/Revenue \| Q1 \| Q2/)).toBeInTheDocument())

    // This is analysis, not editing -- the paid, generative endpoint must never be reached.
    expect(api.aiEditAttachmentImage).not.toHaveBeenCalled()
    expect(api.blurAttachmentRegion).not.toHaveBeenCalled()
    // The transient scratch upload used only to run OCR is cleaned up afterward.
    await waitFor(() => expect(api.deleteAttachment).toHaveBeenCalledWith('scratch-1'))
  })
})

describe('ImageEditor -- stale-response guarding', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('discards an in-flight extract-text result if the modal is closed before it resolves', async () => {
    vi.mocked(api.getAttachmentCapabilities).mockResolvedValue(capabilities())
    vi.mocked(api.uploadAttachments).mockResolvedValue({
      attachments: [
        {
          attachment_id: 'scratch-1',
          filename: 'source.png',
          media_type: 'image/png',
          size_bytes: 10,
          processing_status: 'ready',
          processing_error: null,
        },
      ],
      errors: [],
    })
    vi.mocked(api.deleteAttachment).mockResolvedValue(undefined)
    let resolveExtract!: (value: Awaited<ReturnType<typeof api.extractAttachmentText>>) => void
    vi.mocked(api.extractAttachmentText).mockReturnValue(
      new Promise((resolve) => {
        resolveExtract = resolve
      }),
    )
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <ImageEditor
          open
          onOpenChange={vi.fn()}
          imageSrc="data:image/png;base64,AAAA"
          fileName="photo.png"
          onSave={vi.fn()}
        />
      </QueryClientProvider>,
    )

    const extractButton = await screen.findByRole('button', { name: 'Extract the selected table or chart' })
    await user.click(extractButton)
    await waitFor(() => expect(api.extractAttachmentText).toHaveBeenCalled())

    // Close the modal while the OCR call is still pending.
    rerender(
      <QueryClientProvider client={queryClient}>
        <ImageEditor
          open={false}
          onOpenChange={vi.fn()}
          imageSrc="data:image/png;base64,AAAA"
          fileName="photo.png"
          onSave={vi.fn()}
        />
      </QueryClientProvider>,
    )

    // Reopen for a *different* image before the original request resolves.
    rerender(
      <QueryClientProvider client={queryClient}>
        <ImageEditor
          open
          onOpenChange={vi.fn()}
          imageSrc="data:image/png;base64,BBBB"
          fileName="other.png"
          onSave={vi.fn()}
        />
      </QueryClientProvider>,
    )

    resolveExtract({
      attachment_id: 'scratch-1',
      operation: 'extract_text',
      raw_text: 'stale result text that must never appear',
      cleaned_text: 'stale result text that must never appear',
      regions: [],
      warnings: [],
    })

    // Give the resolved promise's .then chain a turn to run.
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(screen.queryByText(/stale result text/i)).not.toBeInTheDocument()
  })
})
