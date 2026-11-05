import type { paths } from '@/core/api-types.gen';

/**
 * Typed-contract helpers over the generated OpenAPI `paths` (see scripts/generate-api-types.mjs). Feature
 * `api.ts` files derive their DTO types from the backend schema with these instead of hand-copying fields, so a
 * backend change fails `tsc` rather than surfacing at runtime. Reference adopter: modules/observability/api.ts.
 */

type Method = 'get' | 'post' | 'put' | 'patch' | 'delete';

/** JSON body of the successful (200/201) response of `Path` for `M` (default GET). */
export type ApiResponse<P extends keyof paths, M extends Method = 'get'> = paths[P] extends {
  [K in M]: { responses: infer R };
}
  ? R extends { 200: { content: { 'application/json': infer B } } }
    ? B
    : R extends { 201: { content: { 'application/json': infer B } } }
      ? B
      : never
  : never;

/** JSON request body accepted by `Path` for `M`. */
export type ApiBody<P extends keyof paths, M extends Method> = paths[P] extends {
  [K in M]: { requestBody?: { content: { 'application/json': infer B } } };
}
  ? B
  : never;
