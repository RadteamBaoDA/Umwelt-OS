import type { ReactElement } from 'react';

/** Draws a small decorative preview of the renderer's content layout without sample data or provider branding. */
export function GadgetTemplateSample({ renderer }: { renderer: string }) {
  let content: ReactElement;
  switch (renderer) {
    case 'table_panel':
      content = <><path d="M5 11h38M5 18h38M5 25h38M5 32h38M20 8v27M34 8v27" /></>;
      break;
    case 'text_panel':
      content = <><path d="M7 11h31M7 17h36M7 23h29M7 29h34" /></>;
      break;
    case 'video_panel':
      content = <><rect x="6" y="9" width="23" height="21" rx="2" /><path d="m16 15 7 4.5-7 4.5zM33 12h10M33 18h8M33 24h10M6 34h37" /></>;
      break;
    case 'finance_chart':
      content = <><path d="M6 31h37M7 27l9-8 8 4 10-12 8 5" /><circle cx="34" cy="11" r="2" /></>;
      break;
    case 'provider_observation':
      content = <><path d="M7 12h15M7 17h9M7 31h37" /><rect x="29" y="9" width="14" height="12" rx="2" /><path d="M32 15h8" /></>;
      break;
    case 'map':
      content = <><path d="m7 12 11-4 13 4 12-4v23l-12 4-13-4-11 4zM18 8v23M31 12v23" /><circle cx="25" cy="19" r="2" /><circle cx="37" cy="25" r="1.5" /></>;
      break;
    case 'intelligence_panel':
      content = <><path d="M10 13h30M10 19h30M10 25h30M10 31h30" /><circle cx="15" cy="13" r="2" /><circle cx="32" cy="19" r="2" /><circle cx="23" cy="25" r="2" /><circle cx="37" cy="31" r="2" /><path d="m15 13 17 6-9 6 14 6" /></>;
      break;
    case 'tasks':
    case 'goals':
      content = <><rect x="7" y="10" width="5" height="5" rx="1" /><path d="M17 12h24M7 21h5v5H7zM17 23h20M7 32h5v5H7zM17 34h24" /></>;
      break;
    case 'daily_brief':
      content = <><rect x="7" y="9" width="36" height="27" rx="3" /><path d="M12 15h17M12 21h26M12 27h23M12 32h14" /></>;
      break;
    case 'weather':
      content = <><circle cx="17" cy="18" r="6" /><path d="M17 8V6M17 30v-2M7 18H5M29 18h-2M10 11 8.5 9.5M25.5 26.5 24 25M28 31h-5a5 5 0 0 1-.5-10A7 7 0 0 1 36 23a4 4 0 0 1 0 8z" /></>;
      break;
    case 'github_project':
      content = <><circle cx="11" cy="13" r="3" /><circle cx="38" cy="13" r="3" /><circle cx="24" cy="32" r="3" /><path d="M14 14.5 22 29M35 15l-9 14" /><path d="M7 8h10M33 8h10M19 38h10" /></>;
      break;
    default:
      content = <><circle cx="9" cy="12" r="2" /><circle cx="9" cy="21" r="2" /><circle cx="9" cy="30" r="2" /><path d="M15 12h27M15 21h23M15 30h25" /></>;
  }

  return <svg viewBox="0 0 48 42" aria-hidden="true" focusable="false" className="size-14 shrink-0 rounded-md border border-border bg-muted/40 p-1 text-muted-foreground">
    <g fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5">{content}</g>
  </svg>;
}
