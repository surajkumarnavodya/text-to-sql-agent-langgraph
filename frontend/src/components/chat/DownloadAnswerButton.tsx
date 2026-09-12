import { FileDown, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { downloadMarkdownAsPdf } from '@/lib/pdf'

export function DownloadAnswerButton({ question, answer }: { question: string; answer: string }) {
  const { t } = useTranslation()
  const [isGenerating, setIsGenerating] = useState(false)

  const handleDownload = async () => {
    setIsGenerating(true)
    try {
      const slug = question.trim().slice(0, 60).replace(/[^\w\- ]+/g, '').replace(/\s+/g, '-') || 'answer'
      await downloadMarkdownAsPdf(question, answer, `${slug}.pdf`)
    } finally {
      setIsGenerating(false)
    }
  }

  return (
    <Button size="sm" variant="ghost" onClick={() => void handleDownload()} disabled={isGenerating}>
      {isGenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileDown className="h-3.5 w-3.5" />}
      {t('common.downloadAnswer')}
    </Button>
  )
}
