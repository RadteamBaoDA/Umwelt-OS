import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// Vitest globals are off, so Testing Library's automatic cleanup is not registered.
afterEach(() => cleanup());

// Full parallel runs starve jsdom; Testing Library's 1s findBy/waitFor default flakes under that load.
configure({ asyncUtilTimeout: 5000 });

// jsdom lacks the pointer APIs Radix Select/Dialog call.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.setPointerCapture ??= () => undefined;
Element.prototype.releasePointerCapture ??= () => undefined;
Element.prototype.scrollIntoView ??= () => undefined;
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// next/font/google needs the Next compiler; tests only need the loader to return a class name and CSS variable.
vi.mock('next/font/google', () => ({
  Inter: () => ({ className: 'font-inter', variable: '--font-inter' }),
  JetBrains_Mono: () => ({ className: 'font-jetbrains-mono', variable: '--font-mono-skin' }),
}));
