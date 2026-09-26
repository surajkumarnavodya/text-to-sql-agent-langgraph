import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ChatAttachment } from '@/hooks/useChatAttachments'
import { ChatAttachmentChip } from './ChatAttachmentChip'

function makeAttachment(overrides: Partial<ChatAttachment> = {}): ChatAttachment {
  return {
    id: 'a1',
    file: new File(['x'], 'photo.png', { type: 'image/png' }),
    kind: 'image',
    previewUrl: 'blob:preview',
    editedDataUrl: null,
    status: 'ready',
    attachmentId: 'att_1',
    error: null,
    ...overrides,
  }
}

describe('ChatAttachmentChip -- image action menu', () => {
  it('shows the "more actions" menu with Extract text/Resize/Remove text for a ready image', async () => {
    const user = userEvent.setup()
    const onExtractText = vi.fn()
    const onResize = vi.fn()
    const onRemoveText = vi.fn()
    render(
      <ChatAttachmentChip
        attachment={makeAttachment()}
        onRemove={vi.fn()}
        onExtractText={onExtractText}
        onResize={onResize}
        onRemoveText={onRemoveText}
      />,
    )

    await user.click(screen.getByRole('button', { name: /more image actions/i }))
    await user.click(screen.getByText('Extract text'))
    expect(onExtractText).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: /more image actions/i }))
    await user.click(screen.getByText('Resize image'))
    expect(onResize).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: /more image actions/i }))
    await user.click(screen.getByText('Remove text'))
    expect(onRemoveText).toHaveBeenCalledTimes(1)
  })

  it('hides the menu when no image-action handlers are passed', () => {
    render(<ChatAttachmentChip attachment={makeAttachment()} onRemove={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /more image actions/i })).not.toBeInTheDocument()
  })

  it('hides the menu while the attachment is still uploading', () => {
    render(
      <ChatAttachmentChip
        attachment={makeAttachment({ status: 'uploading', attachmentId: null })}
        onRemove={vi.fn()}
        onExtractText={vi.fn()}
        onResize={vi.fn()}
        onRemoveText={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: /more image actions/i })).not.toBeInTheDocument()
  })

  it('hides the menu for a document attachment -- these actions are image-only', () => {
    render(
      <ChatAttachmentChip
        attachment={makeAttachment({
          kind: 'document',
          file: new File(['x'], 'notes.pdf', { type: 'application/pdf' }),
          previewUrl: null,
        })}
        onRemove={vi.fn()}
        onExtractText={vi.fn()}
        onResize={vi.fn()}
        onRemoveText={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: /more image actions/i })).not.toBeInTheDocument()
  })

  it('only shows the menu items for the handlers actually provided', async () => {
    const user = userEvent.setup()
    render(
      <ChatAttachmentChip
        attachment={makeAttachment()}
        onRemove={vi.fn()}
        onExtractText={vi.fn()}
        // onResize/onRemoveText intentionally omitted -- e.g. capabilities
        // reported them unavailable.
      />,
    )
    await user.click(screen.getByRole('button', { name: /more image actions/i }))
    expect(screen.getByText('Extract text')).toBeInTheDocument()
    expect(screen.queryByText('Resize image')).not.toBeInTheDocument()
    expect(screen.queryByText('Remove text')).not.toBeInTheDocument()
  })
})
