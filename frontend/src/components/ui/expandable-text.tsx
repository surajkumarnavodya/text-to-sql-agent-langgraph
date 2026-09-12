import { ChevronDown, ChevronUp } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'

const clampStyle = (lines: number): CSSProperties => ({
  display: '-webkit-box',
  WebkitLineClamp: lines,
  WebkitBoxOrient: 'vertical',
  overflow: 'hidden',
})

/** Clamps long text to `maxLines`, with a bottom fade and a "Show more"
 * toggle when it actually overflows -- the ChatGPT/Perplexity treatment
 * for a long pasted question, so one long turn doesn't push everything
 * else down the page by default.
 *
 * Overflow is measured on a separate, always-clamped, invisible copy of
 * the text (absolutely positioned out of the layout flow) rather than on
 * the visible paragraph itself -- the visible one stops being clamped
 * once expanded, at which point measuring *it* for overflow would always
 * read "not overflowing" and make the toggle disappear right when it's
 * showing the expanded state. Re-measures on window resize so the
 * decision stays correct across breakpoints/zoom levels.
 */
export function ExpandableText({
  text,
  maxLines = 5,
  className,
  textClassName,
  fadeToColor,
  align = 'left',
}: {
  text: string
  maxLines?: number
  className?: string
  textClassName?: string
  /** CSS color (e.g. `var(--accent)` or `var(--card)`) the bottom fade
   * blends into -- should match whatever background this sits on. */
  fadeToColor: string
  align?: 'left' | 'center'
}) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const measureRef = useRef<HTMLParagraphElement>(null)

  useLayoutEffect(() => {
    const el = measureRef.current
    if (!el) return
    const check = () => setOverflowing(el.scrollHeight > el.clientHeight + 1)
    check()
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [text, maxLines])

  return (
    <div className={className}>
      <div className="relative">
        <p
          ref={measureRef}
          aria-hidden="true"
          className={cn('pointer-events-none invisible absolute inset-x-0 top-0 -z-10 whitespace-pre-wrap', textClassName)}
          style={clampStyle(maxLines)}
        >
          {text}
        </p>
        <p
          className={cn('whitespace-pre-wrap', textClassName)}
          style={!expanded ? clampStyle(maxLines) : undefined}
        >
          {text}
        </p>
        {!expanded && overflowing && (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t to-transparent"
            style={{ backgroundImage: `linear-gradient(to top, ${fadeToColor}, transparent)` }}
          />
        )}
      </div>
      {overflowing && (
        <button
          type="button"
          onClick={() => setExpanded((prev) => !prev)}
          className={cn(
            'mt-1 flex items-center gap-1 text-xs font-medium opacity-90 hover:underline hover:opacity-100',
            align === 'center' && 'mx-auto',
          )}
        >
          {expanded ? t('common.showLess') : t('common.showMore')}
          {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
      )}
    </div>
  )
}
