import type { StringCopyKey } from '../copy';

type CopyKey = StringCopyKey;


/** Gallery categories shown as filter pills. */
export type GalleryCategory = 'understand' | 'compare' | 'make';

/** Ordered gallery categories with their catalog label keys. */
export const GALLERY_CATEGORIES: ReadonlyArray<{ id: GalleryCategory; labelKey: CopyKey }> = [
  { id: 'understand', labelKey: 'shellGalleryCatUnderstand' },
  { id: 'compare', labelKey: 'shellGalleryCatCompare' },
  { id: 'make', labelKey: 'shellGalleryCatMake' },
];

/** One gallery example; all copy comes from the shared chat message catalog. */
export interface GalleryDemo {
  id: string;
  category: GalleryCategory;
  titleKey: CopyKey;
  descKey: CopyKey;
  promptKey: CopyKey;
}

/** The seven gallery examples. */
export const GALLERY_DEMOS: readonly GalleryDemo[] = [
  { id: 'plane', category: 'understand', titleKey: 'shellDemoPlaneTitle', descKey: 'shellDemoPlaneDesc', promptKey: 'shellDemoPlanePrompt' },
  { id: 'trip', category: 'understand', titleKey: 'shellDemoTripTitle', descKey: 'shellDemoTripDesc', promptKey: 'shellDemoTripPrompt' },
  { id: 'bicycle', category: 'understand', titleKey: 'shellDemoBikeTitle', descKey: 'shellDemoBikeDesc', promptKey: 'shellDemoBikePrompt' },
  { id: 'monty-hall', category: 'understand', titleKey: 'shellDemoMontyTitle', descKey: 'shellDemoMontyDesc', promptKey: 'shellDemoMontyPrompt' },
  { id: 'weekend', category: 'compare', titleKey: 'shellDemoWeekendTitle', descKey: 'shellDemoWeekendDesc', promptKey: 'shellDemoWeekendPrompt' },
  { id: 'dinner', category: 'make', titleKey: 'shellDemoDinnerTitle', descKey: 'shellDemoDinnerDesc', promptKey: 'shellDemoDinnerPrompt' },
  { id: 'splitter', category: 'make', titleKey: 'shellDemoSplitterTitle', descKey: 'shellDemoSplitterDesc', promptKey: 'shellDemoSplitterPrompt' },
];

/** Demo ids used as starter suggestions on an empty conversation. */
export const STARTER_DEMO_IDS = ['plane', 'trip', 'monty-hall', 'splitter'] as const;
