import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'

/** Renders LLM-generated answer text (web/document/policy answers,
 * synthesized multi-source answers, insight text) as real markdown --
 * bold/headings/lists render properly and links are clickable, instead of
 * showing literal `**`/`[text](url)` characters.
 *
 * Deliberately does NOT enable `rehype-raw` (raw HTML passthrough): this
 * text ultimately comes from web search results or uploaded documents,
 * both explicitly treated as untrusted data elsewhere in this app (see
 * rag/graph.py's and agent/orchestrator/nodes.py's system prompts) -- an
 * embedded HTML/script tag in a poisoned source must render as inert text,
 * never as markup, so react-markdown's plain (HTML-stripping) default
 * behavior is the correct one here, not an oversight.
 */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn('prose prose-sm dark:prose-invert max-w-none', className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: linkChildren }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {linkChildren}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
