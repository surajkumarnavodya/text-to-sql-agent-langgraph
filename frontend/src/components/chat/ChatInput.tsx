import { Send } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'

export function ChatInput({
  onSubmit,
  disabled,
}: {
  onSubmit: (question: string) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSubmit(trimmed)
    setValue('')
  }

  return (
    <div className="flex gap-2 border-t border-[var(--border)] bg-[var(--card)] p-3">
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            submit()
          }
        }}
        placeholder={t('chat.placeholder')}
        disabled={disabled}
        className="h-10 flex-1 rounded-md border border-[var(--border)] bg-[var(--input)] px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
      />
      <Button variant="primary" size="icon" onClick={submit} disabled={disabled || !value.trim()}>
        <Send className="h-4 w-4" />
      </Button>
    </div>
  )
}
