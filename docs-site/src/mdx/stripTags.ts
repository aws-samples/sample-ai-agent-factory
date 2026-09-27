/**
 * Remove every `<...>` sequence from a string by scanning characters instead of a
 * single regex replacement, so nested or split sequences such as `<scr<script>ipt>`
 * cannot survive one pass. Text inside angle brackets is dropped; a `>` outside any
 * bracket is kept as ordinary text. The result is only ever placed in a Markdown text
 * node or a plain title string, which React renders as text, never as HTML.
 */
export function stripTags(value: string): string {
  let out = '';
  let depth = 0;
  for (const ch of value) {
    if (ch === '<') {
      depth += 1;
      continue;
    }
    if (ch === '>' && depth > 0) {
      depth -= 1;
      continue;
    }
    if (depth === 0) out += ch;
  }
  return out;
}
