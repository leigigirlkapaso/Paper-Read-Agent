/**
 * AIContent — reusable React component for rendering AI-generated
 * Markdown with LaTeX math support.
 *
 * Uses react-markdown with remark-math (parses $..$ / $$..$$
 * in markdown source) and rehype-katex (renders as KaTeX HTML).
 *
 * Props:
 *   content   — raw Markdown string (may contain $..$ LaTeX)
 *   className — optional CSS class added to the wrapper div
 */
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { ReactNode } from "react";

export interface AIContentProps {
  content: string;
  className?: string;
}

export function AIContent({ content, className }: AIContentProps): ReactNode {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkMath]}
        rehypePlugins={[rehypeKatex]}
        skipHtml={false}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
