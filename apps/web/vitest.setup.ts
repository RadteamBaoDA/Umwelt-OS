import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// Vitest globals are off, so Testing Library's automatic cleanup is not registered.
afterEach(() => cleanup());

// jsdom lacks the pointer APIs Radix Select/Dialog call.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.setPointerCapture ??= () => undefined;
Element.prototype.releasePointerCapture ??= () => undefined;
Element.prototype.scrollIntoView ??= () => undefined;
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} };

// next/font/google needs the Next compiler; tests only need the loader to return a class name and CSS variable.
vi.mock('next/font/google', () => ({ Inter: () => ({ className: 'font-inter', variable: '--font-inter' }) }));
