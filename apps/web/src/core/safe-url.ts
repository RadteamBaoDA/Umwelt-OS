/** Returns the URL only when it parses as credential-free http(s); connector and document data is untrusted for href use. */
export function safeHttpUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const { protocol, username, password } = new URL(value);
    // Credentialed URLs (https://user:pass@host) are never wanted as hrefs and are a spoofing vector.
    return (protocol === 'http:' || protocol === 'https:') && !username && !password ? value : undefined;
  } catch { return undefined; }
}
