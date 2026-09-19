import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button, type ButtonProps } from '@/components/ui/button'

export interface CopyButtonProps extends Omit<ButtonProps, 'onClick' | 'children'> {
  /** Plain text to copy -- unlike `lib/clipboard.tsx`'s
   * `copyMarkdownToClipboard` (used for answer text, which benefits from
   * a rich-text HTML+plain-text dual write), SQL/code should copy as
   * exactly what's shown, nothing reformatted. */
  getText: () => string
  label?: string
}

/** Shared icon-button copy affordance (SQL editor, future code blocks) --
 * `navigator.clipboard.writeText` only, a brief checkmark confirmation,
 * consistent with CopyAnswerButton's own timing/feedback shape but kept
 * separate since that component is markdown-specific. */
export function CopyButton({ getText, label, className, size = 'icon', variant = 'ghost', ...props }: CopyButtonProps) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(getText())
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  const accessibleLabel = copied ? t('common.copied') : (label ?? t('common.copy'))

  return (
    <Button
      size={size}
      variant={variant}
      onClick={() => void handleCopy()}
      aria-label={accessibleLabel}
      title={accessibleLabel}
      className={className}
      {...props}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-[var(--success)]" /> : <Copy className="h-3.5 w-3.5" />}
    </Button>
  )
}
