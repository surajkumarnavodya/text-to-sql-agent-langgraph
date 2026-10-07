/**
 * Speech-friendly text for "Read aloud". A pure function from the same
 * plain-markdown export Copy and Download already use (`buildAnswerMarkdown`)
 * to an ordered list of punctuated, length-bounded speech chunks.
 *
 * Works on the answer's text, never on the DOM, so nothing on screen (buttons,
 * labels, tooltips, CSS) can leak into speech, and nothing is sent anywhere.
 * The answer itself is never modified: SQL is announced or read, never run.
 */

export interface SpeechOptions {
  /** `summary`: a SQL block is announced, not read. `read`: its text is spoken, capped. */
  sqlMode: 'summary' | 'read'
  /** A table with more body rows than this is summarized on screen, not read row by row. */
  maxTableRows: number
  /** Soft upper bound on the characters in one SpeechSynthesis utterance. */
  maxChunkChars: number
}

export const DEFAULT_SPEECH_OPTIONS: SpeechOptions = {
  sqlMode: 'summary',
  maxTableRows: 20,
  maxChunkChars: 200,
}

const MAX_SQL_SPOKEN_CHARS = 400
const SOURCES_SENTENCE = 'Sources are available for this answer.'
const NUMBER_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']
const SEPARATOR_LINE = /^\s*\|?\s*:?-+:?(\s*\|\s*:?-+:?)*\s*\|?\s*$/

function ensureTerminalPunctuation(text: string): string {
  if (text === '') return ''
  return /[.!?:]$/.test(text) ? text : `${text}.`
}

/** Inline markdown -> plain words, without a trailing period (used for table cells and names). */
function plainInline(text: string): string {
  return text
    .replace(/<[^>]*>/g, ' ')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/gi, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/__(.+?)__/g, '$1')
    .replace(/(^|\s)\*(\S.*?)\*(?=\s|$|[.,;:!?])/g, '$1$2')
    .replace(/(\d)\s?%/g, '$1 percent')
    .replace(/\s+/g, ' ')
    .trim()
}

function inlineToSpeech(text: string): string {
  return ensureTerminalPunctuation(plainInline(text))
}

function joinWords(items: string[]): string {
  if (items.length <= 1) return items.join('')
  if (items.length === 2) return `${items[0]} and ${items[1]}`
  return `${items.slice(0, -1).join(', ')}, and ${items[items.length - 1]}`
}

function codeToSpeech(lang: string, code: string, options: SpeechOptions): string[] {
  const looksLikeSql = lang === 'sql' || /^\s*(with|select|insert|update|delete)\b/i.test(code)
  if (!looksLikeSql) return ['A code block is shown on screen.']
  if (options.sqlMode === 'summary') return ['A SQL query is shown on screen.']

  const collapsed = code.replace(/\s+/g, ' ').trim()
  if (collapsed.length <= MAX_SQL_SPOKEN_CHARS) {
    return [`SQL query follows. ${ensureTerminalPunctuation(collapsed)}`]
  }
  const truncated = collapsed.slice(0, MAX_SQL_SPOKEN_CHARS).replace(/\s+\S*$/, '')
  return [`SQL query follows. ${truncated}. The rest of the query is shown on screen.`]
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

function tableToSpeech(header: string[], body: string[][], options: SpeechOptions): string[] {
  const names = header.map(plainInline)
  const columnCount = names.length
  const columnWord = columnCount <= 10 ? NUMBER_WORDS[columnCount] : String(columnCount)
  const lines = [
    ensureTerminalPunctuation(
      `Table with ${columnWord} column${columnCount === 1 ? '' : 's'}: ${joinWords(names)}`,
    ),
  ]

  if (body.length === 0) {
    lines.push('The table has no rows.')
    return lines
  }

  for (const row of body.slice(0, options.maxTableRows)) {
    const cells = names.map((_, index) => plainInline(row[index] ?? ''))
    if (columnCount === 2) {
      if (cells[0] && cells[1]) lines.push(`${cells[0]}: ${cells[1]}.`)
    } else {
      const pairs = names
        .map((name, index) => (cells[index] ? `${name}: ${cells[index]}` : ''))
        .filter((pair) => pair !== '')
      if (pairs.length > 0) lines.push(ensureTerminalPunctuation(pairs.join(', ')))
    }
  }

  const remaining = body.length - options.maxTableRows
  if (remaining > 0) {
    lines.push(`${remaining} more row${remaining === 1 ? '' : 's'} ${remaining === 1 ? 'is' : 'are'} shown on screen.`)
  }
  return lines
}

/** Converts the answer's markdown into ordered speech blocks (one per paragraph, heading,
 * list item, quote, or summarized code/table block). Unknown markdown degrades to plain text. */
export function markdownToSpeechBlocks(
  markdown: string,
  options: SpeechOptions = DEFAULT_SPEECH_OPTIONS,
): string[] {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n')
  const blocks: string[] = []
  let i = 0

  while (i < lines.length) {
    const trimmed = lines[i].trim()

    if (trimmed.startsWith('```')) {
      const lang = trimmed.slice(3).trim().toLowerCase()
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        code.push(lines[i])
        i++
      }
      i++ // closing fence, or end of input
      blocks.push(...codeToSpeech(lang, code.join('\n'), options))
      continue
    }

    if (trimmed.includes('|') && i + 1 < lines.length && SEPARATOR_LINE.test(lines[i + 1])) {
      const header = splitRow(lines[i])
      const body: string[][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        body.push(splitRow(lines[i]))
        i++
      }
      blocks.push(...tableToSpeech(header, body, options))
      continue
    }

    // buildAnswerMarkdown's own "**Sources:**" label, followed by its list of
    // filenames/URLs. Spoken as one natural sentence; the list itself is never read.
    if (/^\*\*Sources:\*\*$/i.test(trimmed)) {
      i++
      while (i < lines.length && /^[-*+]\s/.test(lines[i].trim())) i++
      blocks.push(SOURCES_SENTENCE)
      continue
    }

    if (trimmed === '' || /^([-*_])(\s*\1){2,}$/.test(trimmed)) {
      i++
      continue
    }

    const heading = /^#{1,6}\s+(.*)$/.exec(trimmed)
    if (heading) {
      blocks.push(inlineToSpeech(heading[1]))
    } else {
      const bullet = /^[-*+]\s+(.*)$/.exec(trimmed)
      const ordered = /^(\d+)[.)]\s+(.*)$/.exec(trimmed)
      const quote = /^>\s?(.*)$/.exec(trimmed)
      if (bullet) blocks.push(inlineToSpeech(bullet[1]))
      else if (ordered) blocks.push(`${ordered[1]}. ${inlineToSpeech(ordered[2])}`)
      else if (quote) blocks.push(inlineToSpeech(quote[1]))
      else blocks.push(inlineToSpeech(trimmed))
    }
    i++
  }

  return blocks.filter((block) => block !== '')
}

/** Splits one block into sentences, then into whole-word pieces no longer than `maxChars`. */
function splitSentences(block: string, maxChars: number): string[] {
  return block.split(/(?<=[.!?])\s+/).flatMap((sentence) => splitAtWords(sentence, maxChars))
}

function splitAtWords(text: string, maxChars: number): string[] {
  const parts: string[] = []
  let rest = text.trim()
  while (rest.length > maxChars) {
    let cut = rest.lastIndexOf(' ', maxChars)
    if (cut <= 0) cut = rest.indexOf(' ')
    // A single token longer than the limit is left whole rather than split mid-word.
    if (cut <= 0) break
    parts.push(rest.slice(0, cut).trim())
    rest = rest.slice(cut).trim()
  }
  if (rest !== '') parts.push(rest)
  return parts
}

/** Greedily packs sentences into chunks of at most `maxChunkChars`, in reading order.
 * Packing stops at a sentence boundary whenever the next sentence would overflow, so
 * chunks break at sentences and never mid-word (except for an over-long single token). */
export function chunkSpeechBlocks(blocks: string[], maxChunkChars: number): string[] {
  const sentences = blocks.flatMap((block) => splitSentences(block, maxChunkChars))
  const chunks: string[] = []
  let current = ''
  for (const sentence of sentences) {
    if (current === '') {
      current = sentence
    } else if (current.length + 1 + sentence.length > maxChunkChars) {
      chunks.push(current)
      current = sentence
    } else {
      current = `${current} ${sentence}`
    }
  }
  if (current !== '') chunks.push(current)
  return chunks
}

/** Full pipeline: markdown answer -> ordered speech chunks ready for SpeechSynthesis.
 * Returns an empty array when there is nothing speakable. */
export function buildSpeechChunks(
  markdown: string,
  options: SpeechOptions = DEFAULT_SPEECH_OPTIONS,
): string[] {
  return chunkSpeechBlocks(markdownToSpeechBlocks(markdown, options), options.maxChunkChars)
}
