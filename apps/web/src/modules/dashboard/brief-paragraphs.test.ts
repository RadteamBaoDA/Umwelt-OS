import { describe, expect, it } from 'vitest';
import { parseBriefParagraphs } from './brief-paragraphs';

describe('parseBriefParagraphs', () => {
  it('splits paragraphs, peels the lead label and extracts citation markers', () => {
    const out = parseBriefParagraphs('Markets. BTC up [1].\n\nWorld. Quake [2]. Deal [3].\n\nYou. One task.');
    expect(out).toHaveLength(3);
    expect(out[0].lead).toBe('Markets.');
    expect(out[0].parts).toEqual([
      { kind: 'text', value: 'BTC up ' },
      { kind: 'cite', ref: 1 },
      { kind: 'text', value: '.' },
    ]);
    expect(out[1].parts.filter((p) => p.kind === 'cite')).toHaveLength(2);
    expect(out[2].lead).toBe('You.');
  });

  it('keeps a paragraph without a lead label whole', () => {
    expect(parseBriefParagraphs('no lead here at all.')[0]).toEqual({
      lead: null,
      parts: [{ kind: 'text', value: 'no lead here at all.' }],
    });
  });
});
