import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';

// public/vendor/genui holds generated third-party bundles (npm run vendor:genui); never lint them.
const eslintConfig = [{ ignores: ['public/vendor/**'] }, ...nextVitals, ...nextTypescript];

export default eslintConfig;
