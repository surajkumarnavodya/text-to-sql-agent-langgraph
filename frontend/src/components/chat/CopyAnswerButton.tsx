import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { copyMarkdownToClipboard } from '@/lib/clipboard'

export function CopyAnswerButton({ answer }: { answer: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await copyMarkdownToClipboard(answer)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Button size="sm" variant="ghost" onClick={() => void handleCopy()}>
      {copied ? <Check className="h-3.5 w-3.5 text-[var(--success)]" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? t('common.copied') : t('common.copyAnswer')}
    </Button>
  )
}
