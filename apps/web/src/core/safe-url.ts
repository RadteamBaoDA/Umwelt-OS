/** Returns the URL only when it parses as http(s); connector and document data is untrusted for href use. */
export function safeHttpUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const { protocol } = new URL(value);
    return protocol === 'http:' || protocol === 'https:' ? value : undefined;
  } catch { return undefined; }
}
