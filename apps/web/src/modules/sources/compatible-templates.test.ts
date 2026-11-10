import { describe, expect, it } from 'vitest';
import { addTemplateHref, compatibleTemplates, type GadgetTemplate } from './compatible-templates';

const tpl = (id: string, renderer: string, accepted: GadgetTemplate['accepted_shapes'], mode: GadgetTemplate['source_mode'] = 'multi') => ({ id, renderer, source_mode: mode, accepted_shapes: accepted }) as GadgetTemplate;

describe('compatibleTemplates', () => {
  const templates = [tpl('news', 'news_feed', ['news']), tpl('chart', 'finance_chart', ['series']), tpl('any', 'text_panel', null), tpl('none', 'map', null, 'none'), tpl('news2', 'news_feed', ['news'])];

  it('keeps templates whose accepted shapes intersect, shape-agnostic ones, and one per renderer', () => {
    expect(compatibleTemplates({ data_shapes: ['news'] }, templates).map((t) => t.id)).toEqual(['news', 'any']);
  });

  it('skips source-less templates and matches only agnostic templates when shapes are absent', () => {
    expect(compatibleTemplates({}, templates).map((t) => t.id)).toEqual(['any']);
  });

  it('builds the dashboard deep link', () => {
    expect(addTemplateHref('a b', 's1')).toBe('/dashboard?addTemplate=a%20b&source=s1');
  });
});
