import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Copies markdown text to the clipboard as both HTML and plain text in
 * one write, so pasting into a rich-text-aware target (email, Slack,
 * Word/Docs) keeps the bold/headings/links formatting, while plain-text
 * targets (a terminal, a bare <textarea>) still get something sensible.
 *
 * `react-dom/server`'s `renderToStaticMarkup` is dynamically imported --
 * it's a genuinely large module (a whole separate React renderer) that
 * only this "Copy answer" path needs; every other markdown rendering in
 * this app (the on-screen <Markdown> component, the PDF export) mounts
 * react-markdown into a real DOM node instead, which is already covered
 * by the client renderer already in the main bundle.
 */
export async function copyMarkdownToClipboard(markdown: string): Promise<void> {
  const { renderToStaticMarkup } = await import('react-dom/server')
  const html = renderToStaticMarkup(<ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>)

  if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([markdown], { type: 'text/plain' }),
        }),
      ])
      return
    } catch {
      // Falls through to the plain-text-only copy below -- e.g. Safari's
      // stricter ClipboardItem permissions, or a non-secure (http, not
      // localhost) context where the rich write is rejected.
    }
  }
  await navigator.clipboard.writeText(markdown)
}
