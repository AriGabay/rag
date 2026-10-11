"use client";

import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CUT_MARKER } from "@/lib/format";

/** Citation ids an answer may carry: passages (S), measurements (M), values (V), user assumptions (A),
 * calculations (C). */
const CITATION_GROUP = /\[((?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*)\]/g;

export interface CitationTarget {
  id: string;
  /** Display number (order of first appearance in the answer). */
  n: number;
  label: string;
  title: string;
}

/** Order of first appearance of every cited id: the chips show these numbers, the details list uses them too. */
export function citationOrder(markdown: string | null | undefined): string[] {
  const out: string[] = [];
  for (const m of (markdown ?? "").matchAll(CITATION_GROUP)) {
    for (const id of m[1].split(/\s*[,،;]\s*/)) {
      if (!out.includes(id)) out.push(id);
    }
  }
  return out;
}

/** The answer text without citation markers (for copying). */
export function plainAnswer(markdown: string): string {
  return markdown.replace(CITATION_GROUP, "").replace(/[ \t]+([.,:;])/g, "$1").trim();
}

function withCitationLinks(markdown: string): string {
  return markdown.replace(CITATION_GROUP, (_all, ids: string) =>
    ids
      .split(/\s*[,،;]\s*/)
      .map((id) => `[${id}](#cite-${id})`)
      .join(""),
  );
}

/** The server's cut markers «[N: חלק שלא אומת הושמט]» become links to `#cut-N`, keeping the marker's own words as the
 * link text, so the displayed text stays the saved text; `components.a` renders them apart from the model's words. */
function withCutLinks(markdown: string): string {
  return markdown.replace(CUT_MARKER, (_all, n: string) => `[«\\[${n}: חלק שלא אומת הושמט\\]»](#cut-${n})`);
}

interface Props {
  markdown: string;
  citations: Map<string, CitationTarget>;
  onCite: (id: string) => void;
  /** The ordinals of the markers that have an entry in the details (round 8 U8); the others are shown, not linked. */
  cuts?: ReadonlySet<number>;
  onCut?: (ordinal: number) => void;
}

function MarkdownImpl({ markdown, citations, onCite, cuts, onCut }: Props) {
  const components = useMemo<Components>(
    () => ({
      a({ href, children }) {
        if (href && href.startsWith("#cite-")) {
          const id = href.slice(6);
          const target = citations.get(id);
          if (!target) return <span className="cite" aria-disabled="true">?</span>;
          return (
            <button
              type="button"
              className="cite"
              title={target.title}
              aria-label={`מקור ${target.n}: ${target.title}`}
              onClick={() => onCite(id)}
            >
              {target.n}
            </button>
          );
        }
        if (href && href.startsWith("#cut-")) {
          const n = Number(href.slice(5));
          if (cuts?.has(n) && onCut) {
            return (
              <button
                type="button"
                className="cut-marker"
                data-testid="cut-marker"
                data-ordinal={n}
                aria-label={`חלק שלא אומת הושמט ${n}: פתיחת הפירוט`}
                onClick={() => onCut(n)}
              >
                {children}
              </button>
            );
          }
          return (
            <span className="cut-marker" data-testid="cut-marker" data-ordinal={n}>
              {children}
            </span>
          );
        }
        // Answers carry no outside links: any other link is shown as its text only.
        return <>{children}</>;
      },
      table({ children }) {
        return (
          <div className="table-wrap">
            <table>{children}</table>
          </div>
        );
      },
      img() {
        return null;
      },
    }),
    [citations, onCite, cuts, onCut],
  );
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>
      {withCutLinks(withCitationLinks(markdown))}
    </ReactMarkdown>
  );
}

export const Markdown = memo(MarkdownImpl);
