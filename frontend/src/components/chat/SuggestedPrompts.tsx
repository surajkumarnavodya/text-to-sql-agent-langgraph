import { useTranslation } from 'react-i18next'

/** Starter prompts shown only on the empty-state screen -- clicking one
 * asks it immediately, the same as typing it and pressing Enter. Kept
 * generic/example-shaped rather than reading this database's own schema,
 * since a suggestion that happens to name a real table would reintroduce
 * exactly the kind of technical label the redesign is removing elsewhere. */
export function SuggestedPrompts({ onSelect }: { onSelect: (prompt: string) => void }) {
  const { t } = useTranslation()
  const prompts = [
    t('workspace.promptRevenue'),
    t('workspace.promptTopProducts'),
    t('workspace.promptRegion'),
    t('workspace.promptDeclining'),
  ]

  return (
    <div className="flex flex-wrap justify-center gap-2">
      {prompts.map((prompt) => (
        <button
          key={prompt}
          type="button"
          onClick={() => onSelect(prompt)}
          className="rounded-full border border-[var(--border)] bg-[var(--card)] px-3.5 py-1.5 text-sm text-[var(--foreground)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        >
          {prompt}
        </button>
      ))}
    </div>
  )
}
