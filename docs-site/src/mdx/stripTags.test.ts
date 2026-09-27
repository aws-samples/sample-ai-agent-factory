import { describe, expect, it } from 'vitest';
import { stripTags } from './stripTags';

describe('stripTags', () => {
  it('removes simple tags and keeps their text', () => {
    expect(stripTags('<b>More</b> screenshots')).toBe('More screenshots');
  });
  it('removes nested and split sequences in one pass', () => {
    expect(stripTags('<scr<script>ipt>alert(1)</script>')).toBe('alert(1)');
    expect(stripTags('<<b>>x<</b>>')).toBe('x');
  });
  it('drops everything after an unclosed opening bracket', () => {
    expect(stripTags('Title <img src=x onerror=alert(1)')).toBe('Title ');
  });
  it('keeps a stray closing bracket as text', () => {
    expect(stripTags('a > b')).toBe('a > b');
  });
  it('never leaves a tag-like sequence behind', () => {
    for (const s of ['<a<b>c>', '<<script>script>', '<s<s<s>>>', 'plain']) {
      expect(stripTags(s)).not.toMatch(/<[^>]*>/);
    }
  });
});
