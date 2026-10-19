import { describe, expect, it } from 'vitest';
import {
  balanceUnclosedTags,
  extractCompleteStyles,
  processPartialHtml,
} from '../sandbox/process-partial-html';

describe('processPartialHtml', () => {
  it('strips an incomplete trailing tag at the end of a streaming chunk', () => {
    expect(processPartialHtml('<div>Hello</div><div cla')).toBe('<div>Hello</div>');
    expect(processPartialHtml('<div>Hello</div><')).toBe('<div>Hello</div>');
  });

  it('strips complete and unterminated <style> blocks', () => {
    expect(processPartialHtml('<style>.a{color:red}</style><div>Hi</div>')).toBe('<div>Hi</div>');
    expect(processPartialHtml('<div>Hi</div><style>.a{color:')).toBe('<div>Hi</div>');
  });

  it('strips complete and unterminated <script> blocks so script code never leaks or executes via HTML', () => {
    expect(processPartialHtml('<script>alert("x")</script><div>Hi</div>')).toBe('<div>Hi</div>');
    expect(processPartialHtml('<div>Hi</div><script>fetch("/secret')).toBe('<div>Hi</div>');
    expect(processPartialHtml('<script src="https://evil.example/payload.js" /><div>Safe</div>')).toBe(
      '<div>Safe</div>',
    );
  });

  it('strips external stylesheet <link>, <iframe>, <object>, and <embed> injections', () => {
    expect(
      processPartialHtml(
        '<link rel="stylesheet" href="https://evil.example/styles.css"><iframe src="https://evil.example"></iframe><div>Clean</div>',
      ),
    ).toBe('<div>Clean</div>');
    expect(processPartialHtml('<div>Clean</div><iframe src="https://evil.example')).toBe(
      '<div>Clean</div>',
    );
  });

  it('neutralizes javascript: URLs in href and src attributes', () => {
    expect(processPartialHtml('<a href="javascript:alert(1)">Click</a>')).toBe(
      '<a href="#">Click</a>',
    );
  });

  it('strips complete and unterminated <head> blocks', () => {
    expect(processPartialHtml('<head><title>T</title></head><div>Hi</div>')).toBe('<div>Hi</div>');
    expect(processPartialHtml('<head><meta charset=')).toBe('');
  });

  it('strips incomplete trailing HTML entities while preserving complete entities', () => {
    expect(processPartialHtml('<div>a &amp', { balanceTags: false })).toBe('<div>a ');
    expect(processPartialHtml('<div>a &#1', { balanceTags: false })).toBe('<div>a ');
    expect(processPartialHtml('<div>a &amp</div>')).toBe('<div>a &amp</div>');
    expect(processPartialHtml('<div>a &amp; b</div>')).toBe('<div>a &amp; b</div>');
  });

  it('extracts body content and drops outer <html>/<body> wrappers', () => {
    expect(processPartialHtml('<html><body class="x"><div>Hi</div></body></html>')).toBe(
      '<div>Hi</div>',
    );
    expect(processPartialHtml('<body><div>Hi</div><p>More</p>')).toBe('<div>Hi</div><p>More</p>');
  });

  it('balances unclosed HTML and SVG tags during progressive streaming', () => {
    expect(processPartialHtml('<div class="card"><h3>Title</h3><p>Streaming text')).toBe(
      '<div class="card"><h3>Title</h3><p>Streaming text</p></div>',
    );
    expect(
      processPartialHtml(
        '<svg viewBox="0 0 100 100"><g class="node"><rect width="20" height="20" /><text class="t">Label',
      ),
    ).toBe(
      '<svg viewBox="0 0 100 100"><g class="node"><rect width="20" height="20" /><text class="t">Label</text></g></svg>',
    );
  });

  it('does not add closing tags for void HTML elements', () => {
    expect(balanceUnclosedTags('<div><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><br><input type="text"><span>Open')).toBe(
      '<div><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><br><input type="text"><span>Open</span></div>',
    );
  });
});

describe('extractCompleteStyles', () => {
  it('returns all complete style tags concatenated', () => {
    const html = '<style>.a{color:red}</style><div>x</div><style media="all">.b{}</style>';
    expect(extractCompleteStyles(html)).toBe(
      '<style>.a{color:red}</style><style media="all">.b{}</style>',
    );
  });

  it('ignores unterminated style blocks', () => {
    expect(extractCompleteStyles('<style>.a{color:')).toBe('');
  });
});
