/** Topic owner DTO and bounded write contracts used by news settings. */

/** Detached topic profile returned by the owner API. Weight is topic importance on a 0–10 scale. */
export type Topic = {
  id: string;
  owner_id: number;
  name: string;
  description: string | null;
  keywords: string[];
  entity_ids: string[];
  is_active: boolean;
  weight: number;
  revision: number;
  created_at: string;
  updated_at: string;
};

/** Topic creation is bounded to 50 keywords of 100 characters and 100 unique entity IDs. */
export type TopicCreate = {
  name: string;
  description?: string | null;
  keywords?: string[];
  entity_ids?: string[];
  is_active?: boolean;
  weight?: number;
};

/** Revision-fenced partial changes; null description clears while entity_ids: [] clears links. */
export type TopicUpdate = {
  expected_revision: number;
  name?: string;
  description?: string | null;
  keywords?: string[];
  entity_ids?: string[];
  is_active?: boolean;
  weight?: number;
};

/** Bounded topic collection response with nullable keyset continuation. */
export type TopicPage = { items: Topic[]; next_cursor: string | null; total: number };

/** Canonical entity reference exposed by the public knowledge API. */
export type TopicEntityOption = { id: string; name: string | null; type: string };
