import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import { ResizeImageDialog } from './ResizeImageDialog'

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, resizeAttachmentImage: vi.fn() }
})

const PRESETS = [{ name: 'medium_800', width: 800, height: 800 }]

describe('ResizeImageDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('sends the entered width to the resize endpoint and shows the result', async () => {
    vi.mocked(api.resizeAttachmentImage).mockResolvedValue({
      attachment_id: 'att_new',
      source_attachment_id: 'att_1',
      operation: 'resize',
      image_data_url: 'data:image/png;base64,AAAA',
      media_type: 'image/png',
      width: 400,
      height: 200,
      original_width: 800,
      original_height: 400,
      size_bytes: 12345,
      warnings: [],
    })
    const onResized = vi.fn()
    const user = userEvent.setup()

    render(
      <ResizeImageDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl={null}
        presets={PRESETS}
        maxDimension={4096}
        onResized={onResized}
      />,
    )

    await user.type(screen.getByLabelText(/width/i), '400')
    await user.click(screen.getByRole('button', { name: /^resize$/i }))

    await waitFor(() =>
      expect(api.resizeAttachmentImage).toHaveBeenCalledWith('att_1', {
        width: 400,
        height: undefined,
        fit: 'contain',
        output_format: undefined,
      }),
    )
    await waitFor(() => expect(screen.getByText(/400 x 200px/)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /attach resized image/i }))
    expect(onResized).toHaveBeenCalledWith(
      expect.objectContaining({ attachment_id: 'att_new', width: 400, height: 200 }),
    )
  })

  it('applying a preset fills both dimensions and unlocks the aspect ratio', async () => {
    const user = userEvent.setup()
    render(
      <ResizeImageDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl={null}
        presets={PRESETS}
        maxDimension={4096}
        onResized={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: '800x800' }))
    expect(screen.getByLabelText(/width/i)).toHaveValue(800)
    expect(screen.getByLabelText(/height/i)).toHaveValue(800)
    expect(screen.getByLabelText(/height/i)).not.toBeDisabled()
  })

  it('shows an error and does not call the API when no dimension is entered', async () => {
    const user = userEvent.setup()
    render(
      <ResizeImageDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl={null}
        presets={[]}
        maxDimension={4096}
        onResized={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: /^resize$/i }))
    expect(screen.getByText(/enter a width or height/i)).toBeInTheDocument()
    expect(api.resizeAttachmentImage).not.toHaveBeenCalled()
  })

  it('surfaces a server error without crashing', async () => {
    vi.mocked(api.resizeAttachmentImage).mockRejectedValue(new Error('boom'))
    const user = userEvent.setup()
    render(
      <ResizeImageDialog
        open
        onOpenChange={vi.fn()}
        attachmentId="att_1"
        filename="photo.png"
        previewUrl={null}
        presets={[]}
        maxDimension={4096}
        onResized={vi.fn()}
      />,
    )

    await user.type(screen.getByLabelText(/width/i), '200')
    await user.click(screen.getByRole('button', { name: /^resize$/i }))

    await waitFor(() => expect(screen.getByText(/couldn.t resize/i)).toBeInTheDocument())
  })
})
