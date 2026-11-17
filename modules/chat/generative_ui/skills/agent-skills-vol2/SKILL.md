---
name: "agent-skills-vol2"
description: "Build responsive, accessible calculators, comparisons, charts, and simulations with the sandbox contract."
allowed-tools: []
---

# Interactive answer implementation

Use this guidance after choosing a custom UI because it helps the task.
render_table handles basic tables; this sandbox
handles charts, diagrams and custom interaction.
Do not turn every answer into a dashboard. Use multiple sections when needed,
including headings, assumptions, units, sources, and instructions inside the UI.

## Design system tokens

Use the Umwelt-OS semantic CSS variables (and Open Generative UI aliases) pre-injected in the sandbox:

- Surfaces: `var(--bg)`, `var(--surface)`, `var(--color-background-primary)`, `var(--color-background-secondary)`
- Text: `var(--text)`, `var(--muted)`, `var(--color-text-primary)`, `var(--color-text-secondary)`
- Borders & accents: `var(--line)`, `var(--accent)`, `var(--danger)`, `var(--color-border-tertiary)`
- Charts: `var(--chart-1)`, `var(--chart-2)`, `var(--chart-3)`, `var(--chart-4)`, `var(--chart-5)`

## Streaming and execution

For render_sandboxed_ui emit initialHeight, placeholderMessages, css, html,
jsFunctions, jsExpressions in that exact order. The placeholder remains until css is complete; put all styles in the css parameter. Keep html meaningful before
scripts run. jsFunctions holds named declarations; jsExpressions holds small
synchronous invocations. There is no API to append expressions to prior calls.

No localStorage, sessionStorage, cookies, IndexedDB, same-origin fetch, parent DOM,
or invented service endpoints. The pre-injected importmap supports bare imports
for three, gsap, d3, and chart.js. jsFunctions/jsExpressions are classic scripts;
top-level await is invalid. Import inside an async function and handle failure:

```js
async function setupScene() {
  const THREE = await import('three');
  const { OrbitControls } = await import(
    "three/examples/jsm/controls/OrbitControls.js"
  );
  // Create geometry, materials, lights, camera, controls, and a responsive renderer.
}
function showSetupError() {
  document.getElementById("status").textContent =
    "The visual could not load. Please retry.";
}
```

A jsExpression can invoke `setupScene().catch(showSetupError);`. The html must
include an initially readable explanation and an element with id="status".
A `<script type="module">` can use bare imports in html when needed. Do not use
static import declarations or top-level await in either classic script channel.
Only the vendored same-origin libraries (three, d3, chart.js, gsap, leaflet) are
available. The sandbox has no network: no CDN scripts, remote images/fonts, or fetch.

## Data and calculations

Use user inputs or actual tool data. Label illustrative numbers as sample data in
the UI. query_data is a bundled dataset, not current metrics. A chart or weather card
does not fetch evidence. Never invent live data or source links.

Check empty inputs, finite numbers, domain limits, and denominators. HTML min/max
alone do not validate values typed by users. A numeric-input example:

```js
function readAmount() {
  const input = document.getElementById("amount");
  const amount = input.valueAsNumber;
  if (!Number.isFinite(amount) || amount < 0 || amount > 1000000) {
    document.getElementById("result").textContent =
      "Enter an amount from 0 to 1,000,000.";
    input.setAttribute("aria-invalid", "true");
    return null;
  }
  input.removeAttribute("aria-invalid");
  return amount;
}
```

Provide a visible label for amount and a role="status" result. Prevent calculations
when validation fails. Keep full internal precision; format outputs with
Intl.NumberFormat or appropriate decimal places. State formula, units, and limits.
Do not evaluate user-entered formulas with eval. Reset defaults and results together.

## Controls and layout

Use real buttons, associated input labels, keyboard focus, and status feedback.
Forms are allowed if submit prevents navigation; buttons that do not submit need
type="button". For tabbed interfaces implement keyboard/ARIA behavior or use simple
buttons with aria-pressed instead of claiming tab semantics. Sort using a button
inside the header and keep sorting/filtering derived from the same source data.
Render user/retrieved strings with textContent, never interpolated HTML.

Use theme variables for text, surfaces, and borders. Resolve canvas colors via
getComputedStyle when needed. Responsive grids should collapse without overflow:
`grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr))`.
Use bounded chart containers and resize observers; do not repeatedly recreate a
chart on every input. Provide text summaries or data tables for canvas and SVG.
Use labels and patterns as well as color. Respect reduced-motion preferences.

## Agent follow-ups

Filtering, sorting, sliders, and computation stay local. A user-clicked action can
send a concise request containing validated current selections through
`await Websandbox.connection.remote.sendPrompt({ text })` (wired to the host `window.sendPrompt` postMessage bridge). Disable the button while
awaiting, catch errors into a visible retryable status, and restore it in finally.
Never trigger a message on load or an input-change event. For external links use
`Websandbox.connection.remote.openLink({ url })` with an HTTPS URL.

## Live geographic maps and animated routes

Use a vendored Leaflet map (no tile layer; the sandbox has no network) or an inline
SVG schematic map for places and geographic exploration. Draw geometry yourself and
never request remote map tiles.
For animated routes, use `window.createTripAnimator` with `pinElements`, Pause/Resume, and Replay controls.
