/** Turns a machine error code such as `invalid_credential` into sentence-case words ("Invalid credential") for display. */
export function humanizeCode(code: string): string {
  const words = code.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
