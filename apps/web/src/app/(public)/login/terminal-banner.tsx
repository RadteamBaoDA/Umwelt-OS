/**
 * Terminal-skin ASCII wordmark for the login and setup cards, copied verbatim from the design demo (`sp-banner`).
 * `String.raw` keeps the backslashes and the leading spaces that JSX text would collapse.
 */
const BANNER = String.raw`  _   _               _ _        ___  ___
 | | | |_ __ ___ _ _ | | |_     / _ \/ __|
 | |_| | '  \ V  V / -_) |  _|  | (_) \__ \
  \___/|_|_|_\_/\_/\___|_|\__|   \___/|___/`;

/**
 * Decorative wordmark shown only in the terminal skin (`public-skin.css` hides `.sp-banner` otherwise). Hidden from
 * assistive technology: the card heading carries the page name.
 */
export function TerminalBanner() {
  return (
    <pre className="sp-banner" aria-hidden="true">
      {BANNER}
    </pre>
  );
}
