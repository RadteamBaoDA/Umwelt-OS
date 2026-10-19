import type { StringCopyKey } from '../copy';

type CopyKey = StringCopyKey;

/** Built-in widget template: static, network-free sandbox markup. */
export interface SeedHtmlTemplate {
  /** Stable identifier. */
  id: string;
  /** Catalog key of the localized title. */
  titleKey: CopyKey;
  /** Catalog key of the localized description. */
  descKey: CopyKey;
  /** Sandbox payload fields (html/css only: no scripts, no network, fictional data). */
  sandbox: { title: string; css: string; html: string };
}

const BASE_CSS =
  'body{margin:0;font-family:system-ui,sans-serif;background:var(--bg,#fff);color:var(--text,#111)}' +
  '.card{padding:16px;border:1px solid #ccc;border-radius:12px;max-width:420px}' +
  '.row{display:flex;justify-content:space-between;padding:4px 0}.muted{color:#666}' +
  '.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.big{font-size:28px;font-weight:600}';

/** The three built-in templates (Weather New York, Invoice Northwind Labs, Q1 KPI dashboard). */
export const SEED_HTML_TEMPLATES: readonly SeedHtmlTemplate[] = [
  {
    id: 'seed-weather-ny',
    titleKey: 'shellTplWeatherTitle',
    descKey: 'shellTplWeatherDesc',
    sandbox: {
      title: 'Weather: New York',
      css: BASE_CSS,
      html:
        '<div class="card"><div class="muted">New York, NY</div><div class="big">64&deg;F</div>' +
        '<div>Partly cloudy</div><div class="row muted"><span>Humidity 58%</span><span>Wind 9 mph NW</span></div>' +
        '<div class="row"><span>Mon 66&deg;</span><span>Tue 61&deg;</span><span>Wed 59&deg;</span></div></div>',
    },
  },
  {
    id: 'seed-invoice-northwind',
    titleKey: 'shellTplInvoiceTitle',
    descKey: 'shellTplInvoiceDesc',
    sandbox: {
      title: 'Invoice: Northwind Labs',
      css: BASE_CSS,
      html:
        '<div class="card"><div class="big">Northwind Labs</div><div class="muted">Invoice #1042</div><hr>' +
        '<div class="row"><span>Design sprint</span><span>$2,400.00</span></div>' +
        '<div class="row"><span>Prototype build</span><span>$3,100.00</span></div>' +
        '<div class="row"><span>Hosting (3 mo)</span><span>$180.00</span></div><hr>' +
        '<div class="row"><strong>Total</strong><strong>$5,680.00</strong></div></div>',
    },
  },
  {
    id: 'seed-kpi-q1',
    titleKey: 'shellTplKpiTitle',
    descKey: 'shellTplKpiDesc',
    sandbox: {
      title: 'Q1 KPI dashboard',
      css: BASE_CSS,
      html:
        '<div class="grid">' +
        '<div class="card"><div class="muted">Revenue</div><div class="big">$1.24M</div><div>+8.2%</div></div>' +
        '<div class="card"><div class="muted">Active users</div><div class="big">48.3k</div><div>+5.1%</div></div>' +
        '<div class="card"><div class="muted">Churn</div><div class="big">2.4%</div><div>-0.3 pt</div></div>' +
        '<div class="card"><div class="muted">NPS</div><div class="big">51</div><div>+4</div></div></div>',
    },
  },
];
