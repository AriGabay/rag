"use client";

import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CUT_MARKER, cutMarkerText } from "@/lib/format";

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
  return markdown.replace(CUT_MARKER, (_all, n: string) => `[${cutMarkerText(n).replace(/[[\]]/g, "\\$&")}](#cut-${n})`);
}

interface Props {
  markdown: string;
  citations: Map<string, CitationTarget>;
  onCite: (id: string) => void;
  /** The ordinals of the markers that have an entry in the details (round 8 U8); the others are shown, not linked. */
  cuts?: ReadonlySet<number>;
  onCut?: (ordinal: number) => void;
  /** Round 14 U8 (KTD9): the ordinals of the "אומת חלקית" marks that open the verification detail; the caller has turned each one
   * into a `#partial-N` link (`linkPartialMarks`) only for a viewer who may open it, so a mark not in the set is shown as text. */
  partials?: ReadonlySet<number>;
  onPartial?: (ordinal: number) => void;
}

function MarkdownImpl({ markdown, citations, onCite, cuts, onCut, partials, onPartial }: Props) {
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
        if (href && href.startsWith("#partial-")) {
          const n = Number(href.slice(9));
          if (partials?.has(n) && onPartial) {
            return (
              <button
                type="button"
                className="cut-marker partial-mark"
                data-testid="partial-mark"
                data-ordinal={n}
                aria-label={`אומת חלקית ${n}: פתיחת הפירוט`}
                onClick={() => onPartial(n)}
              >
                {children}
              </button>
            );
          }
          return <>{children}</>;
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
    [citations, onCite, cuts, onCut, partials, onPartial],
  );
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>
      {withCutLinks(withCitationLinks(markdown))}
    </ReactMarkdown>
  );
}

export const Markdown = memo(MarkdownImpl);
