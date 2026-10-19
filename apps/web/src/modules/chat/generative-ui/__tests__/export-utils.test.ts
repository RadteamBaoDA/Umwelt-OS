import { describe, expect, it, vi } from 'vitest';
import {
  assembleStandaloneHtmlFromActivity,
  buildStandaloneHtmlDocument,
  downloadHtmlDocument,
} from '../sandbox/export-utils';

describe('buildStandaloneHtmlDocument & downloadHtmlDocument', () => {
  const sampleContent = {
    css: 'body{--marker:gen-css}',
    html: ['<div>Hello</div>', '<p>World</p>'],
    jsFunctions: "function greet() { return 'hi'; }",
    jsExpressions: ['greet();', "console.log('done');"],
  };

  it('orders importmap before design-system css before generated css', () => {
    const doc = buildStandaloneHtmlDocument(sampleContent, 'My Widget');
    const importmapIdx = doc.indexOf('<script type="importmap">');
    const designSystemIdx = doc.indexOf('--color-background-primary');
    const svgIdx = doc.indexOf('svg text.t');
    const staggerIdx = doc.indexOf('fadeSlideIn');
    const generatedCssIdx = doc.indexOf('--marker:gen-css');

    expect(doc.startsWith('<!DOCTYPE html>')).toBe(true);
    expect(importmapIdx).toBeGreaterThanOrEqual(0);
    expect(designSystemIdx).toBeGreaterThan(importmapIdx);
    expect(svgIdx).toBeGreaterThan(importmapIdx);
    expect(staggerIdx).toBeGreaterThan(importmapIdx);
    expect(generatedCssIdx).toBeGreaterThan(designSystemIdx);
  });

  it('joins html chunks or single html string into the body after </head>', () => {
    const doc = assembleStandaloneHtmlFromActivity(sampleContent, 'My Widget');
    expect(doc).toContain('<div>Hello</div><p>World</p>');
    expect(doc.indexOf('<div>Hello</div>')).toBeGreaterThan(doc.indexOf('</head>'));

    const singleStringDoc = buildStandaloneHtmlDocument({
      html: '<section>Single</section>',
      jsExpressions: 'greet();',
    });
    expect(singleStringDoc).toContain('<section>Single</section>');
    expect(singleStringDoc).toContain('greet();');
  });

  it('emits jsFunctions at top level of a classic script and jsExpressions in an async IIFE', () => {
    const doc = buildStandaloneHtmlDocument(sampleContent, 'My Widget');
    expect(doc).not.toContain('<script type="module">');
    const iifeIdx = doc.indexOf('(async () => {');
    const fnIdx = doc.indexOf('function greet()');
    const expr1Idx = doc.indexOf('greet();');
    const expr2Idx = doc.indexOf("console.log('done');");

    expect(fnIdx).toBeGreaterThanOrEqual(0);
    expect(iifeIdx).toBeGreaterThan(fnIdx);
    expect(expr1Idx).toBeGreaterThan(iifeIdx);
    expect(expr2Idx).toBeGreaterThan(expr1Idx);
  });

  it('includes graceful sendPrompt and Websandbox bridge stubs and TripAnimator', () => {
    const doc = buildStandaloneHtmlDocument(sampleContent, 'My Widget');
    expect(doc).toContain('window.sendPrompt');
    expect(doc).toContain('window.Websandbox');
    expect(doc).toContain('window.createTripAnimator =');
  });

  it('escapes closing script/style sequences and HTML-escapes the document title', () => {
    const doc = buildStandaloneHtmlDocument(
      {
        css: 'body{color:red}</style><script>alert(1)</script>',
        html: '<div></div>',
        jsFunctions: 'const s = "</script>";',
        jsExpressions: 'document.title = "</script>";',
      },
      '<b>Unsafe Title</b>',
    );
    expect(doc).not.toContain('</style><script>alert(1)</script>');
    expect(doc).toContain('body{color:red}<\\/style>');
    expect(doc).toContain('const s = "<\\/script>";');
    expect(doc).toContain('document.title = "<\\/script>";');
    expect(doc).toContain('<title>&lt;b&gt;Unsafe Title&lt;/b&gt;</title>');
  });

  it('triggers a Blob file download and revokes the object URL', () => {
    const createObjectUrlSpy = vi
      .spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:umwelt-test');
    const revokeObjectUrlSpy = vi
      .spyOn(URL, 'revokeObjectURL')
      .mockImplementation(() => undefined);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);

    downloadHtmlDocument('<!DOCTYPE html><html></html>', 'Portfolio Allocation 2026');

    expect(createObjectUrlSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith('blob:umwelt-test');

    createObjectUrlSpy.mockRestore();
    revokeObjectUrlSpy.mockRestore();
    clickSpy.mockRestore();
  });
});
