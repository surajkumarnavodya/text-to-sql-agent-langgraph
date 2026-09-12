import ReactMarkdown from 'react-markdown'
import { createRoot } from 'react-dom/client'
import remarkGfm from 'remark-gfm'

/** Renders `markdown` with the same react-markdown/remark-gfm pipeline the
 * app's on-screen <Markdown> component uses (headings, bold, lists, links
 * all come out the same way) into an off-screen container, rasterizes it,
 * and saves it as a paginated PDF.
 *
 * Deliberately does NOT reuse src/components/ui/markdown.tsx directly: that
 * component's className hardcodes `dark:prose-invert`, and our `dark:`
 * variant matches *any* descendant of `[data-theme='dark']` on <html> --
 * setting a `data-theme="light"` attribute on this container would NOT
 * stop that ancestor match (CSS descendant selectors don't get reset by an
 * intermediate element), so if dark mode were active the export would
 * silently render light/white prose text on the forced white PDF
 * background. Inlining the same react-markdown setup without that class
 * sidesteps the whole problem: this container's typography colors come
 * only from the explicit inline CSS variables set below, never from
 * ambient dark-mode state.
 *
 * `jspdf`/`html2canvas-pro` are dynamically imported so their weight is
 * never paid by the initial page load -- only by whoever actually clicks
 * "Download answer". html2canvas-pro (a maintained html2canvas fork) is
 * used specifically because our theme's CSS uses modern color functions
 * (color-mix()) the original, unmaintained html2canvas can't parse.
 */
export async function downloadMarkdownAsPdf(
  question: string,
  markdown: string,
  filename: string,
): Promise<void> {
  const [{ default: jsPDF }, { default: html2canvas }] = await Promise.all([
    import('jspdf'),
    import('html2canvas-pro'),
  ])

  const container = document.createElement('div')
  container.style.position = 'fixed'
  container.style.left = '-9999px'
  container.style.top = '0'
  container.style.width = '720px'
  container.style.padding = '40px'
  container.style.background = '#ffffff'
  container.style.color = '#0f172a'
  container.style.fontFamily = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif'
  // index.css's own `.prose { --tw-prose-body: var(--foreground); ... }`
  // rule (which overrides the typography plugin's own baseline defaults)
  // means overriding these *base* design tokens -- not the derived
  // `--tw-prose-*` variables directly, which `.prose`'s rule would just
  // re-derive from --foreground/--accent again and discard -- is what
  // actually pins this container to a light palette independent of the
  // app's current theme. See the module docstring above for why a
  // `data-theme="light"` attribute alone wouldn't do this.
  container.style.setProperty('--background', '#ffffff')
  container.style.setProperty('--foreground', '#0f172a')
  container.style.setProperty('--muted-foreground', '#64748b')
  container.style.setProperty('--muted', '#f1f5f9')
  container.style.setProperty('--accent', '#4f46e5')
  container.style.setProperty('--border', '#e2e8f0')
  document.body.appendChild(container)

  const root = createRoot(container)
  try {
    await new Promise<void>((resolve) => {
      root.render(
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 20, lineHeight: 1.3 }}>{question}</h1>
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
          </div>
        </div>,
      )
      // Two rAFs: one for React to commit the render, one for the browser
      // to complete layout, before html2canvas reads it.
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    })

    const canvas = await html2canvas(container, { backgroundColor: '#ffffff', scale: 2 })
    const imageData = canvas.toDataURL('image/png')

    const pdf = new jsPDF({ unit: 'pt', format: 'a4' })
    const pageWidth = pdf.internal.pageSize.getWidth()
    const pageHeight = pdf.internal.pageSize.getHeight()
    const imageWidth = pageWidth
    const imageHeight = (canvas.height * imageWidth) / canvas.width

    let heightRemaining = imageHeight
    let positionY = 0

    pdf.addImage(imageData, 'PNG', 0, positionY, imageWidth, imageHeight)
    heightRemaining -= pageHeight

    while (heightRemaining > 0) {
      positionY = heightRemaining - imageHeight
      pdf.addPage()
      pdf.addImage(imageData, 'PNG', 0, positionY, imageWidth, imageHeight)
      heightRemaining -= pageHeight
    }

    pdf.save(filename)
  } finally {
    root.unmount()
    document.body.removeChild(container)
  }
}
