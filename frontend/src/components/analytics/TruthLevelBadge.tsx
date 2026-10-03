import { CheckCircle2, ShieldCheck, Sparkles } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { TruthLevel } from '@/lib/types'

/** Prompt 30 -- every analytics claim shows its truth level, never implied.
 * Master rules 9-10: a computed value (database_fact), this platform's own
 * suggestion (ai_inference), and a human-approved definition
 * (confirmed_business_truth) are different kinds of claim and must look
 * different wherever they appear. */
const LEVELS: Record<TruthLevel, { label: string; tone: 'success' | 'warning' | 'accent'; icon: typeof CheckCircle2 }> = {
  database_fact: { label: 'Computed from data', tone: 'success', icon: CheckCircle2 },
  ai_inference: { label: 'AI estimate', tone: 'warning', icon: Sparkles },
  confirmed_business_truth: { label: 'Approved definition', tone: 'accent', icon: ShieldCheck },
}

export function TruthLevelBadge({ level }: { level: TruthLevel }) {
  const { label, tone, icon: Icon } = LEVELS[level]
  return (
    <Badge tone={tone} data-truth-level={level}>
      <Icon className="h-3 w-3" aria-hidden="true" />
      {label}
    </Badge>
  )
}
