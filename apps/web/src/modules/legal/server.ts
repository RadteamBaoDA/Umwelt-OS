// Server-only entry of the legal module. `policy-loader` imports `server-only` and node:fs, so it must never be reachable
// from the client-safe module index (`@/modules/legal`); Server Component pages import `loadPolicy` from here.
export { loadPolicy } from './policy-loader';
