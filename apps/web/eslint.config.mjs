import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';

/** Literal hex colors bypass the semantic design tokens (docs/DESIGN_SYSTEM.md); flagged in className/style strings. */
const HEX_COLOR = '#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\\b';
const HEX_MESSAGE = 'Use a semantic design token (bg, surface, text, muted, line, accent, danger) instead of a literal hex color.';

/** Quality gate rules (plan 3.3 R0): function size, complexity, module boundaries and no literal hex colours. */
const qualityRules = {
  files: ['src/**/*.{ts,tsx}'],
  ignores: ['src/**/*.test.{ts,tsx}', 'src/test/**', 'src/core/api-types.gen.ts'],
  rules: {
    'max-lines-per-function': ['error', { max: 100, skipBlankLines: true, skipComments: true }],
    complexity: ['error', 15],
    // Modules integrate through their index entry point only: `@/modules/<name>` or relative imports inside the module.
    'no-restricted-imports': [
      'error',
      {
        patterns: [
          {
            regex: '^@/modules/[^/]+/(?!server$).+',
            message: 'Import another module through its public index (`@/modules/<name>`), not a deep path.',
          },
        ],
      },
    ],
    'no-restricted-syntax': [
      'error',
      { selector: `JSXAttribute[name.name='className'] Literal[value=/${HEX_COLOR}/]`, message: HEX_MESSAGE },
      { selector: `JSXAttribute[name.name='className'] TemplateElement[value.raw=/${HEX_COLOR}/]`, message: HEX_MESSAGE },
      { selector: `JSXAttribute[name.name='style'] Literal[value=/${HEX_COLOR}/]`, message: HEX_MESSAGE },
      { selector: `JSXAttribute[name.name='style'] TemplateElement[value.raw=/${HEX_COLOR}/]`, message: HEX_MESSAGE },
    ],
  },
};

// public/vendor/genui holds generated third-party bundles (npm run vendor:genui); never lint them.
const eslintConfig = [{ ignores: ['public/vendor/**'] }, ...nextVitals, ...nextTypescript, qualityRules];

export default eslintConfig;
