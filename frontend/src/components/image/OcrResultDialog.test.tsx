import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import { OcrResultDialog } from './OcrResultDialog'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, extractAttachmentText: vi.fn() }
})

describe('OcrResultDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a loading state, then the exact recognized text -- never a paraphrase', async () => {
    vi.mocked(api.extractAttachmentText).mockResolvedValue({
      attachment_id: 'att_1',
      operation: 'extract_text',
      raw_text: 'Invoice #12345\nTotal: $99.00',
      cleaned_text: 'Invoice #12345\nTotal: $99.00',
      regions: [],
      warnings: [],
    })

    render(<OcrResultDialog open attachmentId="att_1" filename="receipt.png" onOpenChange={vi.fn()} />)

    expect(screen.getByText(/reading text/i)).toBeInTheDocument()

    await waitFor(() => expect(screen.getByText(/Invoice #12345/)).toBeInTheDocument())
    expect(screen.getByText(/Total: \$99\.00/)).toBeInTheDocument()
    expect(api.extractAttachmentText).toHaveBeenCalledWith('att_1')
  })

  it('shows an honest "no text detected" message when OCR found nothing', async () => {
    vi.mocked(api.extractAttachmentText).mockResolvedValue({
      attachment_id: 'att_1',
      operation: 'extract_text',
      raw_text: '',
      cleaned_text: '',
      regions: [],
      warnings: ['No text was detected in this image.'],
    })

    render(<OcrResultDialog open attachmentId="att_1" filename="blank.png" onOpenChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/no text was detected/i)).toBeInTheDocument())
  })

  it('surfaces the OCR-unavailable warning honestly rather than crashing', async () => {
    vi.mocked(api.extractAttachmentText).mockResolvedValue({
      attachment_id: 'att_1',
      operation: 'extract_text',
      raw_text: '',
      cleaned_text: '',
      regions: [],
      warnings: ["OCR is not available on this server."],
    })

    render(<OcrResultDialog open attachmentId="att_1" filename="photo.png" onOpenChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/ocr is not available/i)).toBeInTheDocument())
  })

  it('shows a network-failure error state distinct from the "nothing found" state', async () => {
    vi.mocked(api.extractAttachmentText).mockRejectedValue(new Error('network down'))

    render(<OcrResultDialog open attachmentId="att_1" filename="photo.png" onOpenChange={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/couldn.t read text/i)).toBeInTheDocument())
  })

  it('does not fetch when the dialog is closed', () => {
    render(<OcrResultDialog open={false} attachmentId="att_1" filename="photo.png" onOpenChange={vi.fn()} />)
    expect(api.extractAttachmentText).not.toHaveBeenCalled()
  })
})
