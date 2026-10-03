import { describe, expect, it } from 'vitest'
import {
  canExpire,
  canRecordVerdict,
  canResolve,
  describeEvent,
  formatConfidence,
  isTerminal,
  ownerLabel,
  statusTone,
} from './recommendationDisplay'
import type { RecommendationFeedbackEventOut, RecommendationStatus } from './types'

const ALL_STATUSES: RecommendationStatus[] = [
  'generated',
  'reviewed',
  'accepted',
  'rejected',
  'partially_useful',
  'incorrect',
  'resolved',
  'expired',
]

function event(overrides: Partial<RecommendationFeedbackEventOut>): RecommendationFeedbackEventOut {
  return {
    id: 'e1',
    recommendation_id: 'r1',
    from_status: 'accepted',
    to_status: 'resolved',
    event_type: 'status_change',
    actor_user_id: null,
    actor_label: null,
    reason: null,
    recommendation_version: '1.0.0',
    evidence_version: 'abc',
    created_at: '2026-10-03T10:00:00Z',
    ...overrides,
  }
}

describe('lifecycle visibility rules', () => {
  it('offers verdicts only while the record is still awaiting a decision', () => {
    expect(ALL_STATUSES.filter(canRecordVerdict)).toEqual(['generated', 'reviewed'])
  })

  it('offers resolve only from accepted or partially useful', () => {
    expect(ALL_STATUSES.filter(canResolve)).toEqual(['accepted', 'partially_useful'])
  })

  it('treats exactly the four no-outgoing-transition statuses as terminal', () => {
    expect(ALL_STATUSES.filter(isTerminal)).toEqual(['rejected', 'incorrect', 'resolved', 'expired'])
  })

  it('never offers expire on a terminal record', () => {
    for (const status of ALL_STATUSES) {
      expect(canExpire(status)).toBe(!isTerminal(status))
    }
  })

  it('a record that can still take a verdict is never terminal', () => {
    for (const status of ALL_STATUSES.filter(canRecordVerdict)) {
      expect(isTerminal(status)).toBe(false)
    }
  })

  it('maps every status to a tone', () => {
    for (const status of ALL_STATUSES) {
      expect(['neutral', 'success', 'warning', 'danger', 'accent']).toContain(statusTone(status))
    }
  })
})

describe('describeEvent', () => {
  it('names the first event as generated, not as a transition from nothing', () => {
    expect(describeEvent(event({ from_status: null, to_status: 'generated' }))).toBe('Generated')
  })

  it('describes a status change as from -> to', () => {
    expect(describeEvent(event({ from_status: 'accepted', to_status: 'resolved' }))).toBe(
      'Accepted → Resolved',
    )
  })

  it('describes a note by its kind, never as a status change', () => {
    expect(
      describeEvent(event({ event_type: 'note', from_status: 'generated', to_status: 'generated' })),
    ).toBe('Note added')
  })

  it('distinguishes assigning from clearing an owner', () => {
    expect(
      describeEvent(
        event({
          event_type: 'owner_assigned',
          from_status: 'generated',
          to_status: 'generated',
          detail: { owner_user_id: 'u1', previous_owner_user_id: null },
        }),
      ),
    ).toBe('Owner assigned')
    expect(
      describeEvent(
        event({
          event_type: 'owner_assigned',
          from_status: 'generated',
          to_status: 'generated',
          detail: { owner_user_id: null, previous_owner_user_id: 'u1' },
        }),
      ),
    ).toBe('Owner cleared')
  })

  it('treats an event without event_type as a status change (pre-Prompt-31 clients)', () => {
    expect(describeEvent(event({ event_type: undefined, from_status: null, to_status: 'generated' }))).toBe(
      'Generated',
    )
  })
})

describe('ownerLabel', () => {
  it('says unassigned when there is no owner', () => {
    expect(ownerLabel({ owner_user_id: null, owner_display_name: null })).toBe('Unassigned')
  })

  it('uses the display name when one exists', () => {
    expect(ownerLabel({ owner_user_id: 'u1', owner_display_name: 'Ola' })).toBe('Ola')
  })

  it('never falls back to an email-shaped string when no name is set', () => {
    const label = ownerLabel({ owner_user_id: 'u1', owner_display_name: null })
    expect(label).toBe('Assigned to a reviewer')
    expect(label).not.toContain('@')
  })
})

describe('formatConfidence', () => {
  it('rounds to a whole percent', () => {
    expect(formatConfidence(0.8)).toBe('80%')
    expect(formatConfidence(0.654)).toBe('65%')
  })

  it('says not scored rather than showing 0% when there is no score', () => {
    expect(formatConfidence(null)).toBe('Not scored')
    expect(formatConfidence(Number.NaN)).toBe('Not scored')
  })
})
