import { describe, expect, it } from 'vitest';
import {
  currencyPrefix,
  formatTerminalChange,
  formatTerminalCompact,
  formatTerminalPercent,
  formatTerminalValue,
  terminalObservationLayout,
} from './observation-terminal';

describe('formatTerminalValue', () => {
  it('prints full precision with two fraction digits', () => {
    expect(formatTerminalValue(71340.2, 'en-us')).toBe('71,340.20');
    expect(formatTerminalValue(73.42, 'en-us')).toBe('73.42');
  });

  it('keeps four digits under one and can drop the fraction for the chart figure', () => {
    expect(formatTerminalValue(0.0671, 'en-us')).toBe('0.0671');
    expect(formatTerminalValue(71340.2, 'en-us', true)).toBe('71,340');
    expect(formatTerminalValue(73.42, 'en-us', true)).toBe('73.42');
  });

  it('formats by locale and shows an em dash for missing values', () => {
    expect(formatTerminalValue(1284.55, 'vi-vi')).toBe('1.284,55');
    expect(formatTerminalValue(null, 'en-us')).toBe('—');
    expect(formatTerminalValue(Number.NaN, 'en-us')).toBe('—');
  });
});

describe('formatTerminalChange', () => {
  it('prints arrow, sign and two decimals', () => {
    expect(formatTerminalChange(3.123, 'up')).toEqual({ text: '▲ +3.12%', tone: 'positive' });
    expect(formatTerminalChange(-0.62, 'up')).toEqual({ text: '▼ −0.62%', tone: 'negative' });
  });

  it('inverts the tone when lower is better and colours neutral metrics by sign', () => {
    expect(formatTerminalChange(2, 'down').tone).toBe('negative');
    expect(formatTerminalChange(-2, 'down').tone).toBe('positive');
    expect(formatTerminalChange(2, 'neutral').tone).toBe('positive');
    expect(formatTerminalChange(-2, 'neutral').tone).toBe('negative');
  });

  it('handles flat and unknown changes', () => {
    expect(formatTerminalChange(0, 'up')).toEqual({ text: '0.00%', tone: 'neutral' });
    expect(formatTerminalChange(null, 'up')).toEqual({ text: '—', tone: 'neutral' });
  });
});

describe('terminal sparkline rows', () => {
  it('picks the layout from the series count', () => {
    expect(terminalObservationLayout(1)).toBe('figure');
    expect(terminalObservationLayout(2)).toBe('sparkrows');
    expect(terminalObservationLayout(3)).toBe('sparkrows');
    expect(terminalObservationLayout(7)).toBe('table');
  });

  it('formats compact values, percent without arrow and currency prefix', () => {
    expect(formatTerminalCompact(71340.2, 'USD', 'en-us')).toBe('71.3K');
    expect(formatTerminalCompact(20.52, '°C', 'en-us')).toBe('20.5°');
    expect(formatTerminalPercent(3.12, 'up')).toEqual({ text: '+3.1%', tone: 'positive' });
    expect(formatTerminalPercent(-1.4, 'up').text).toBe('−1.4%');
    expect(currencyPrefix('USD', 'en-us')).toBe('$');
    expect(currencyPrefix(null, 'en-us')).toBe('');
  });
});
