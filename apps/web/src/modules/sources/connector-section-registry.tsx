'use client';

import type { ComponentType } from 'react';
import type { ConnectorCatalogEntry } from './api';
import {
  ApiKeyField,
  BearerTokenField,
  GoogleConnect,
  RedditConnect,
  RestAuthFields,
  TelegramCredential,
} from './connector-connect-sections';
import { isGoogleProvider } from './connector-oauth';
import { isNativeEntry, isPreset } from './connector-editor-model';
import type { ConnectorEditorModel } from './connector-editor-types';
import {
  FixedScope,
  GoogleScopePicker,
  HealthScope,
  HomeAssistantScope,
  NativeScope,
  RssScope,
  UrlScope,
} from './connector-scope-sections';

/** A section component of the connector editor; it reads everything it needs from the shared view model. */
export type EditorSection = ComponentType<{ m: ConnectorEditorModel }>;

/** Provider families that have a dedicated Connect-step section list; every other provider uses `default`. */
export type ConnectFamily = 'google' | 'reddit' | 'telegram' | 'rest' | 'default';
/** Provider families that have a dedicated Choose-data section. */
export type ScopeFamily = 'google' | 'health' | 'home_assistant' | 'fixed' | 'native' | 'rss' | 'url';

/**
 * Connect-step sections per provider family, in render order. Each section still decides internally whether it applies (the
 * bearer-token and API-key fields are capability-driven and can apply to any family), so the DOM is identical to the former
 * unconditional list. A new provider family plugs in by adding an entry here and a family rule below; the editor is not edited.
 */
export const CONNECT_SECTIONS: Record<ConnectFamily, readonly EditorSection[]> = {
  google: [GoogleConnect, BearerTokenField, ApiKeyField],
  reddit: [RedditConnect, BearerTokenField, ApiKeyField],
  telegram: [BearerTokenField, TelegramCredential, ApiKeyField],
  rest: [BearerTokenField, ApiKeyField, RestAuthFields],
  default: [BearerTokenField, ApiKeyField],
};

/** Choose-data section per provider family; the family rules in `scopeFamily` pick which one a provider uses. */
export const SCOPE_SECTIONS: Record<ScopeFamily, EditorSection> = {
  google: GoogleScopePicker,
  health: HealthScope,
  home_assistant: HomeAssistantScope,
  fixed: FixedScope,
  native: NativeScope,
  rss: RssScope,
  url: UrlScope,
};

/** Connect-step family of a provider: Google (mail, calendar, drive), Reddit, Telegram, REST, else the capability-driven default. */
export function connectFamily(provider: string): ConnectFamily {
  if (isGoogleProvider(provider)) return 'google';
  if (provider === 'reddit' || provider === 'telegram' || provider === 'rest') return provider;
  return 'default';
}

/**
 * Choose-data family of a provider, evaluated in the former chain order: Google, health, Home Assistant, fixed-scope presets,
 * registered native providers, RSS, then the generic URL input.
 */
export function scopeFamily(provider: string, entry: ConnectorCatalogEntry | undefined): ScopeFamily {
  if (isGoogleProvider(provider)) return 'google';
  if (provider === 'health' || provider === 'home_assistant') return provider;
  if (entry && isPreset(entry) && entry.scope_fields.length === 0) return 'fixed';
  if (isNativeEntry(entry)) return 'native';
  return provider === 'rss' ? 'rss' : 'url';
}
