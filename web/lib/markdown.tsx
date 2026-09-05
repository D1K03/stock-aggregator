import type { ReactNode } from "react";

/* A deliberately small subset of Markdown, rendered to React elements.
 *
 * Not react-markdown, and not `dangerouslySetInnerHTML`. Steven's output is a
 * few short paragraphs with the occasional bold word, bullet list or inline
 * code span; a full parser is several packages for a fraction of its grammar.
 * Building React nodes rather than an HTML string means model output cannot
 * inject markup at all, which matters more here than completeness.
 *
 * What is not supported renders as the literal characters, which is a
 * graceful way to be wrong.
 *
 * Headings are deliberately flattened to bold text at the normal size. A chat
 * bubble is not a document, and an `##` turning into 24px type in a 400px
 * panel looks broken rather than emphatic. */

/** `**bold**`, `*italic*` and `` `code` `` within one line. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // One pass, alternating between the markers so nesting cannot confuse it.
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyPrefix}-i${i++}`;
    if (token.startsWith("**")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    last = match.index + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function renderMarkdown(source: string): ReactNode[] {
  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let paragraph: string[] = [];
  let key = 0;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const text = paragraph.join(" ");
    blocks.push(<p key={`p${key++}`}>{inline(text, `p${key}`)}</p>);
    paragraph = [];
  };

  const flushList = () => {
    if (!list) return;
    const items = list.items.map((item, i) => (
      <li key={`li${i}`}>{inline(item, `l${key}-${i}`)}</li>
    ));
    blocks.push(
      list.ordered ? <ol key={`o${key++}`}>{items}</ol> : <ul key={`u${key++}`}>{items}</ul>
    );
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }

    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const heading = /^\s*#{1,6}\s+(.*)$/.exec(line);

    if (bullet) {
      flushParagraph();
      if (!list || list.ordered) {
        flushList();
        list = { ordered: false, items: [] };
      }
      list.items.push(bullet[1]);
      continue;
    }

    if (numbered) {
      flushParagraph();
      if (!list || !list.ordered) {
        flushList();
        list = { ordered: true, items: [] };
      }
      list.items.push(numbered[1]);
      continue;
    }

    if (heading) {
      flushParagraph();
      flushList();
      // Flattened on purpose: emphasis without the size.
      blocks.push(
        <p key={`h${key++}`}>
          <strong>{inline(heading[1], `h${key}`)}</strong>
        </p>
      );
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();
  return blocks;
}
