import { FileDown } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { downloadMarkdown } from '@/lib/csv'

export function DownloadAnswerButton({ question, answer }: { question: string; answer: string }) {
  const { t } = useTranslation()
  const handleDownload = () => {
    const slug = question.trim().slice(0, 60).replace(/[^\w\- ]+/g, '').replace(/\s+/g, '-') || 'answer'
    downloadMarkdown(`# ${question}\n\n${answer}\n`, `${slug}.md`)
  }
  return (
    <Button size="sm" variant="ghost" onClick={handleDownload}>
      <FileDown className="h-3.5 w-3.5" />
      {t('common.downloadAnswer')}
    </Button>
  )
}
