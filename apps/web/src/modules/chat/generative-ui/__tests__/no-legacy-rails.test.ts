import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const CHAT_MODULE_ROOT = path.resolve(__dirname, '../..');

/**
 * Recursively collects all TypeScript/TSX source files under a directory.
 */
function collectSourceFiles(dir: string): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...collectSourceFiles(fullPath));
    } else if (/\.(ts|tsx)$/.test(entry.name) && !entry.name.endsWith('.test.ts') && !entry.name.endsWith('.test.tsx')) {
      files.push(fullPath);
    }
  }
  return files;
}

describe('Chat Generative UI Security & Legacy Rail Guards', () => {
  it('preserves strict HTML/tag sanitization in chat-markdown.tsx', () => {
    const markdownPath = path.join(CHAT_MODULE_ROOT, 'chat-markdown.tsx');
    const source = fs.readFileSync(markdownPath, 'utf-8');

    expect(source).toContain('html: false');
    for (const forbiddenTag of ['img', 'iframe', 'object', 'script', 'style', 'svg', 'math']) {
      expect(source).toContain(`'${forbiddenTag}'`);
    }
    expect(source).toContain('FORBID_TAGS');
  });

  it('ensures no @copilotkit/* or provider-keys imports exist anywhere in apps/web/src/modules/chat/', () => {
    const files = collectSourceFiles(CHAT_MODULE_ROOT);
    expect(files.length).toBeGreaterThan(0);

    for (const filePath of files) {
      const content = fs.readFileSync(filePath, 'utf-8');
      expect(content).not.toMatch(/from\s+['"]@copilotkit\//);
      expect(content).not.toMatch(/import\s*\(\s*['"]@copilotkit\//);
      expect(content).not.toMatch(/provider-keys/);
    }
  });
});

