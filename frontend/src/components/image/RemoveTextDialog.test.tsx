import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import { RemoveTextDialog } from './RemoveTextDialog'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    detectAttachmentTextRegions: vi.fn(),
    removeAttachmentText: vi.fn(),
  }
})

describe('RemoveTextDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('auto-detects regions on open and removes the selected ones', async () => {
    vi.mocked(api.detectAttachmentTextRegions).mockResolvedValue({
      attachment_id: 'att_1',
      regions: [{ region_id: 0, text: 'Hello', left: 10, top: 10, width: 50, height: 20, confidence: 90 }],
    })
    vi.mocked(api.removeAttachmentText).mockResolvedValue({
      attachment_id: 'att_edited',
      source_attachment_id: 'att_1',
      operation: 'remove_text',
      image_data_url: 'data:image/png;base64,BBBB',
      media_type: 'image/png',
      width: 200,
      height: 150,
      original_width: null,
      original_height: null,
      size_bytes: 999,
      warnings: ['Text removal uses classical inpainting (OpenCV’s Telea algorithm), not a generative AI model.'],
    })
    const onEdited = vi.fn()
    const user = userEvent.setup()

    render(
      <RemoveTextDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="sign.png"
        previewUrl="blob:sign"
        maxRegions={20}
        onEdited={onEdited}
      />,
    )

    await waitFor(() => expect(api.detectAttachmentTextRegions).toHaveBeenCalledWith('att_1'))
    await waitFor(() => expect(screen.getByText(/confirm or adjust/i)).toBeInTheDocument())

    const image = screen.getByAltText('')
    Object.defineProperty(image, 'naturalWidth', { value: 200, configurable: true })
    Object.defineProperty(image, 'naturalHeight', { value: 150, configurable: true })
    fireEvent.load(image)

    await user.click(screen.getByRole('button', { name: /^remove text$/i }))

    await waitFor(() =>
      expect(api.removeAttachmentText).toHaveBeenCalledWith('att_1', [
        { left: 10, top: 10, width: 50, height: 20 },
      ]),
    )
    await waitFor(() => expect(screen.getByText(/classical inpainting/i)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /attach edited image/i }))
    expect(onEdited).toHaveBeenCalledWith(expect.objectContaining({ attachment_id: 'att_edited' }))
  })

  it('deselecting the only detected region disables the remove button', async () => {
    vi.mocked(api.detectAttachmentTextRegions).mockResolvedValue({
      attachment_id: 'att_1',
      regions: [{ region_id: 0, text: 'Hello', left: 10, top: 10, width: 50, height: 20, confidence: 90 }],
    })
    const user = userEvent.setup()

    render(
      <RemoveTextDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="sign.png"
        previewUrl="blob:sign"
        maxRegions={20}
        onEdited={vi.fn()}
      />,
    )

    await waitFor(() => expect(screen.getByTitle('Hello')).toBeInTheDocument())
    await user.click(screen.getByTitle('Hello'))

    expect(screen.getByRole('button', { name: /^remove text$/i })).toBeDisabled()
  })

  it('falls open to a manual-selection hint when nothing was auto-detected', async () => {
    vi.mocked(api.detectAttachmentTextRegions).mockResolvedValue({ attachment_id: 'att_1', regions: [] })

    render(
      <RemoveTextDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl="blob:photo"
        maxRegions={20}
        onEdited={vi.fn()}
      />,
    )

    await waitFor(() => expect(screen.getByText(/no text was automatically detected/i)).toBeInTheDocument())
  })

  it('a network failure while detecting still leaves the dialog usable (manual mode)', async () => {
    vi.mocked(api.detectAttachmentTextRegions).mockRejectedValue(new Error('network down'))

    render(
      <RemoveTextDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl="blob:photo"
        maxRegions={20}
        onEdited={vi.fn()}
      />,
    )

    await waitFor(() => expect(screen.getByText(/no text was automatically detected/i)).toBeInTheDocument())
  })
})
