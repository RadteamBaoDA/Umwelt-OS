---
name: "svg-diagram-skill"
description: "Generating rich inline SVG diagrams to visually explain systems, processes, architectures, and abstract concepts."
allowed-tools: []
---

# SVG Diagram Generation Skill

You can generate rich, inline SVG diagrams to visually explain concepts. Use this skill whenever a visual would help the user understand a system, process, architecture, or mechanism better than text alone.

---

## Delivery

Diagrams ship through the `render_sandboxed_ui` tool: the SVG markup goes in the `html` parameter, where it streams to the user element by element. SVG presentation attributes (`fill`, `stroke`, `font-size`) carry all styling here — if a diagram needs CSS animation classes, declare them in the `css` parameter, never in a `<style>` block inside `html`. Static diagrams need no `jsFunctions` or `jsExpressions`.

---

## When to Use

- Explaining how something works (load paths, circuits, pipelines, algorithms)
- Showing architecture or structure (system diagrams, component layouts)
- Illustrating processes or flows (flowcharts, data flow, decision trees)
- Building intuition for abstract concepts (attention mechanisms, gradient descent, recursion)

## SVG Setup

Use this template as a starting point; add an accessible title/description:

```svg
<svg width="100%" viewBox="0 0 680 H" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
            markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke"
            stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
  </defs>
  <!-- Content here -->
</svg>
```

- **680px is a useful default coordinate width** via viewBox. Set `width="100%"` so it scales responsively.
- **H (height)** = bottom-most element's y + height + 40px padding. Don't guess — compute it.
- **Safe content area**: x=40 to x=640, y=40 to y=(H-40).
- **Body fragments only**: no `<html>`, `<head>`, `<body>`, or DOCTYPE.
- **Background is transparent** — the host provides the background.

---

## Core Design Rules

### Typography

- **Two sizes only**: 14px for titles/labels, 12px for subtitles/descriptions.
- **Two weights only**: 400 (regular), 500 (medium/bold). Never use 600 or 700.
- **Font**: Use `font-family="system-ui, -apple-system, sans-serif"` or inherit from host.
- **Always set** `text-anchor="middle"` and `dominant-baseline="central"` for centered text in boxes.
- **Sentence case always**. Never Title Case or ALL CAPS.

### Text Width Estimation

At 14px, each character ~ 8px wide. At 12px, each character ~ 7px wide.

- "Load Balancer" (13 chars) at 14px ~ 104px -> needs rect ~ 140px wide (with padding).
- Always compute: `rect_width = max(title_chars x 8, subtitle_chars x 7) + 48px padding`.

### Colors (Light/Dark Mode Safe)

Use these semantic color sets that work in both modes:

```
Teal:    fill="#E1F5EE" stroke="#0F6E56" text="#085041"  (dark: fill="#085041" stroke="#5DCAA5" text="#9FE1CB")
Purple:  fill="#EEEDFE" stroke="#534AB7" text="#3C3489"  (dark: fill="#3C3489" stroke="#AFA9EC" text="#CECBF6")
Coral:   fill="#FAECE7" stroke="#993C1D" text="#712B13"  (dark: fill="#712B13" stroke="#F0997B" text="#F5C4B3")
Amber:   fill="#FAEEDA" stroke="#854F0B" text="#633806"  (dark: fill="#633806" stroke="#EF9F27" text="#FAC775")
Blue:    fill="#E6F1FB" stroke="#185FA5" text="#0C447C"  (dark: fill="#0C447C" stroke="#85B7EB" text="#B5D4F4")
Gray:    fill="#F1EFE8" stroke="#5F5E5A" text="#444441"  (dark: fill="#444441" stroke="#B4B2A9" text="#D3D1C7")
Red:     fill="#FCEBEB" stroke="#A32D2D" text="#791F1F"  (dark: fill="#791F1F" stroke="#F09595" text="#F7C1C1")
Green:   fill="#EAF3DE" stroke="#3B6D11" text="#27500A"  (dark: fill="#27500A" stroke="#97C459" text="#C0DD97")
Pink:    fill="#FBEAF0" stroke="#993556" text="#72243E"  (dark: fill="#72243E" stroke="#ED93B1" text="#F4C0D1")
```

**Color meaning, not sequence**: Don't rainbow-cycle. Use 2-3 colors per diagram. Map colors to categories or physical properties (warm = heat/energy, cool = calm/cold, gray = structural/neutral).

If you're rendering inside a system that supports CSS variables, prefer the Umwelt-OS and Open Generative UI tokens:

- `var(--text)` / `var(--color-text-primary)` for primary text
- `var(--muted)` / `var(--color-text-secondary)` for muted text
- `var(--line)` / `var(--color-border-tertiary)` for light borders
- `var(--bg)` and `var(--surface)` for surfaces, `var(--accent)` and `var(--danger)` for highlights
- `var(--chart-1)`, `var(--chart-2)`, `var(--chart-3)`, `var(--chart-4)`, `var(--chart-5)` for series accents
- Interactive follow-ups from SVG controls use `Websandbox.connection.remote.sendPrompt({ text })` (wired to `window.sendPrompt`).

### Shapes & Layout

- **Stroke width**: 0.5px for borders, 1.5px for arrows/connectors.
- **Corner radius**: `rx="4"` for subtle rounding, `rx="8"` for emphasized. `rx="20"` for large containers.
- **Spacing**: 60px minimum between boxes, 24px padding inside boxes, 12px text-to-edge clearance.
- **Single-line box**: 44px tall. **Two-line box**: 56px tall.
- **Max 4-5 nodes per row** at 680px width. If more, split into multiple diagrams.
- **All connectors need `fill="none"`** — SVG defaults fill to black, which turns paths into black blobs.

---

## Component Patterns

### Single-Line Node

```svg
<g>
  <rect x="100" y="20" width="180" height="44" rx="8"
        fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>
  <text x="190" y="42" text-anchor="middle" dominant-baseline="central"
        font-size="14" font-weight="500" fill="#3C3489">Node title</text>
</g>
```

### Two-Line Node

```svg
<g>
  <rect x="100" y="20" width="200" height="56" rx="8"
        fill="#E6F1FB" stroke="#185FA5" stroke-width="0.5"/>
  <text x="200" y="38" text-anchor="middle" dominant-baseline="central"
        font-size="14" font-weight="500" fill="#0C447C">Title</text>
  <text x="200" y="56" text-anchor="middle" dominant-baseline="central"
        font-size="12" fill="#185FA5">Short subtitle</text>
</g>
```

### Arrow Connector

```svg
<line x1="200" y1="76" x2="200" y2="120"
      stroke="#534AB7" stroke-width="1.5" marker-end="url(#arrow)"/>
```

---

## Critical Checks Before Finalizing

1. **ViewBox height**: Find your lowest element (max y + height). Set H = that + 40px.
2. **No content past x=640 or below y=(H-40)**.
3. **Text fits in boxes**: `(char_count x 8) + 48 < rect_width` for 14px text.
4. **No arrows through boxes**: Trace every line's path — if it crosses a rect, reroute.
5. **All `<path>` connectors have `fill="none"`**.
6. **All text has appropriate fill color** — never rely on inheritance (SVG defaults to black).
7. **Colors work in dark mode**: If using hardcoded colors, provide both light and dark variants. If using CSS variables, you're fine.

## Task and accessibility checks

Use a diagram when it improves understanding; plain text and native components are
also valid answers. No mandatory planning or narration sequence is required.
Use headings and concise explanations inside multi-section answers when useful.
Provide an SVG title/description or adjacent textual equivalent. Keep text readable
at narrow widths; simplify or reflow dense diagrams instead of shrinking labels.
Do not invent data. Label illustrative quantities and show units and sources.
Use keyboard-operable HTML controls for interactions, and honor reduced motion.
