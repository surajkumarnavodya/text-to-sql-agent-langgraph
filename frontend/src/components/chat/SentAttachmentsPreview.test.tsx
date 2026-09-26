import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SentAttachmentPreview } from '@/lib/history'
import { SentAttachmentsPreview } from './SentAttachmentsPreview'

describe('SentAttachmentsPreview', () => {
  it('renders nothing for a turn with no attachments -- the overwhelming common case', () => {
    const { container } = render(<SentAttachmentsPreview attachments={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it(
    'renders an image thumbnail for a sent image attachment -- closing the real ' +
      'gap where a sent question with an image showed no record of it in the ' +
      'transcript at all',
    () => {
      const attachments: SentAttachmentPreview[] = [
        { filename: 'cow.png', kind: 'image', previewUrl: 'blob:fake-url' },
      ]
      render(<SentAttachmentsPreview attachments={attachments} />)

      const image = screen.getByAltText('cow.png')
      expect(image).toHaveAttribute('src', 'blob:fake-url')
    },
  )

  it('renders a filename chip (no thumbnail) for a sent document attachment', () => {
    const attachments: SentAttachmentPreview[] = [
      { filename: 'report.pdf', kind: 'document', previewUrl: null },
    ]
    render(<SentAttachmentsPreview attachments={attachments} />)

    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('falls back to a filename chip for an image with no preview URL', () => {
    const attachments: SentAttachmentPreview[] = [
      { filename: 'mystery.png', kind: 'image', previewUrl: null },
    ]
    render(<SentAttachmentsPreview attachments={attachments} />)

    expect(screen.getByText('mystery.png')).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('renders multiple attachments from the same turn', () => {
    const attachments: SentAttachmentPreview[] = [
      { filename: 'a.png', kind: 'image', previewUrl: 'blob:a' },
      { filename: 'b.pdf', kind: 'document', previewUrl: null },
    ]
    render(<SentAttachmentsPreview attachments={attachments} />)

    expect(screen.getByAltText('a.png')).toBeInTheDocument()
    expect(screen.getByText('b.pdf')).toBeInTheDocument()
  })
})
