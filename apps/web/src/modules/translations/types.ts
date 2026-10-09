/** Wire contracts of /api/v1/translations and /api/v1/settings/translation. */
export type ResourceType = 'news_story' | 'daily_brief';
export type TranslationStatus = 'pending' | 'ready' | 'unchanged' | 'blocked' | 'failed';
export type TranslationLanguage = 'vi' | 'en';

export type TranslationSettings = { enabled: boolean; target_language: TranslationLanguage; configuration_revision: number };
export type TranslationSettingsUpdate = { enabled: boolean; target_language: TranslationLanguage; expected_revision: number };

export type TranslationItemRequest = { resource_type: ResourceType; resource_id: string; resource_revision: string };
export type TranslationPayload = { title?: string | null; excerpt?: string | null; content?: string | null };
export type TranslationItemStatus = { resource_type: ResourceType; resource_id: string; status: TranslationStatus };
export type TranslationBatchAccepted = { batch_id: string | null; items: TranslationItemStatus[]; settings: TranslationSettings };
export type TranslationItem = TranslationItemStatus & {
  translation: TranslationPayload | null; target_language: TranslationLanguage; original_revision: string; error_code: string | null;
};
export type TranslationBatch = { batch_id: string; target_language: TranslationLanguage; items: TranslationItem[] };

/** Result the UI keeps per resource; absent means "show the original". */
export type TranslationResult = { status: TranslationStatus; translation: TranslationPayload | null; errorCode?: string | null };
