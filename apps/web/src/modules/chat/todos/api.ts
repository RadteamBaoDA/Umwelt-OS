import { apiRequest, csrfHeaders } from '@/core/api';

/** Maximum todos one conversation may hold (mirrors the backend bound). */
export const MAX_TODOS = 50;

/** One conversation todo as returned by the API. */
export interface ChatTodo {
  id: string;
  title: string;
  description: string;
  emoji: string;
  status: 'pending' | 'completed';
  position: number;
}

/** Writable todo fields; a non-UUID or missing `id` makes the server assign one. */
export type ChatTodoInput = Pick<ChatTodo, 'title' | 'description' | 'emoji' | 'status'> & { id?: string };

/**
 * TanStack Query key of one conversation's todo list.
 *
 * @param conversationId - Conversation owning the todos.
 * @returns Stable query key.
 */
export const todosKey = (conversationId: string) => ['chat', 'todos', conversationId] as const;

const base = (conversationId: string) => `/api/v1/conversations/${conversationId}/todos`;

/**
 * Lists a conversation's todos in display order.
 *
 * @param conversationId - Conversation owning the todos.
 * @returns Todos ordered by position.
 */
export function listTodos(conversationId: string): Promise<ChatTodo[]> {
  return apiRequest<ChatTodo[]>(base(conversationId));
}

/**
 * Atomically replaces the whole todo list (max 50).
 *
 * @param conversationId - Conversation owning the todos.
 * @param todos - Complete desired list in order.
 * @param csrfToken - Session CSRF token.
 * @returns The stored list with server-assigned ids.
 */
export function replaceTodos(conversationId: string, todos: ChatTodoInput[], csrfToken: string): Promise<ChatTodo[]> {
  return apiRequest<ChatTodo[]>(base(conversationId), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ todos }),
  });
}

/**
 * Updates one todo's status and/or title.
 *
 * @param conversationId - Conversation owning the todo.
 * @param todoId - Todo to change.
 * @param patch - Fields to change.
 * @param csrfToken - Session CSRF token.
 * @returns The updated todo.
 */
export function patchTodo(
  conversationId: string,
  todoId: string,
  patch: { status?: ChatTodo['status']; title?: string },
  csrfToken: string,
): Promise<ChatTodo> {
  return apiRequest<ChatTodo>(`${base(conversationId)}/${todoId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify(patch),
  });
}
