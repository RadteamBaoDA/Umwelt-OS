'use client';

import * as React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDownIcon, ChevronUpIcon, PencilIcon, PlusIcon, Trash2Icon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { MAX_TODOS, listTodos, patchTodo, replaceTodos, todosKey, type ChatTodo } from './api';
import { uuid7 } from '@/core/uuid7';

/** Props of {@link TodosPanel}. */
export interface TodosPanelProps {
  /** Conversation whose todos are shown; the panel renders nothing without one. */
  conversationId: string | null | undefined;
  /** True while an assistant response streams; the list refetches when it turns false (the model may have changed it). */
  isStreaming?: boolean;
}

/**
 * Optimistic todo editing for one conversation: add, edit, complete/reopen and delete.
 *
 * Every change updates the cache immediately and is rolled back with `failed` set when the request fails; successful
 * replies replace the cache. Concurrent in-flight queries are cancelled first so a stale refetch cannot overwrite the
 * optimistic state.
 *
 * @param conversationId - Conversation owning the list.
 * @param todos - Current cached todos.
 * @returns Draft/edit state plus the `toggle`, `remove`, `add` and `saveEdit` commands.
 */
function useTodoActions(conversationId: string, todos: ChatTodo[]) {
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [draft, setDraft] = React.useState('');
  const [editing, setEditing] = React.useState<{ id: string; title: string } | null>(null);
  const [failed, setFailed] = React.useState(false);
  const key = todosKey(conversationId);

  /** Apply `next` optimistically, run the request, then store the server result or roll back. */
  async function mutate(next: ChatTodo[], request: () => Promise<ChatTodo[] | ChatTodo>) {
    await queryClient.cancelQueries({ queryKey: key });
    const previous = queryClient.getQueryData<ChatTodo[]>(key);
    setFailed(false);
    queryClient.setQueryData(key, next);
    try {
      const result = await request();
      if (Array.isArray(result)) queryClient.setQueryData(key, result);
      else queryClient.setQueryData<ChatTodo[]>(key, (cur) => (cur ?? []).map((x) => (x.id === result.id ? result : x)));
    } catch {
      queryClient.setQueryData(key, previous);
      setFailed(true);
    }
  }

  const input = (todo: ChatTodo) => ({ id: todo.id, title: todo.title, description: todo.description, emoji: todo.emoji, status: todo.status });
  const toggle = (todo: ChatTodo) => {
    const status = todo.status === 'completed' ? 'pending' : 'completed';
    void mutate(todos.map((x) => (x.id === todo.id ? { ...x, status } : x)), () => patchTodo(conversationId, todo.id, { status }, session.csrfToken));
  };
  const remove = (todo: ChatTodo) => {
    const rest = todos.filter((x) => x.id !== todo.id);
    void mutate(rest, () => replaceTodos(conversationId, rest.map(input), session.csrfToken));
  };
  const add = () => {
    const title = draft.trim();
    if (!title || todos.length >= MAX_TODOS) return;
    setDraft('');
    const created: ChatTodo = { id: `tmp-${uuid7()}`, title, description: '', emoji: '', status: 'pending', position: todos.length };
    void mutate([...todos, created], () => replaceTodos(conversationId, [...todos.map(input), { title, description: '', emoji: '', status: 'pending' }], session.csrfToken));
  };
  const saveEdit = () => {
    if (!editing) return;
    const title = editing.title.trim();
    const target = todos.find((x) => x.id === editing.id);
    setEditing(null);
    if (!title || !target || title === target.title) return;
    void mutate(todos.map((x) => (x.id === target.id ? { ...x, title } : x)), () => patchTodo(conversationId, target.id, { title }, session.csrfToken));
  };
  return { draft, setDraft, editing, setEditing, failed, toggle, remove, add, saveEdit };
}

/** One todo row: checkbox plus either the inline edit form or the title/description with edit and remove buttons. */
function TodoItem({ todo, actions }: { todo: ChatTodo; actions: ReturnType<typeof useTodoActions> }) {
  const t = useTranslations('todos');
  const { editing, setEditing, toggle, remove, saveEdit } = actions;
  return (
    <li className="flex items-start gap-2">
      <Checkbox
        checked={todo.status === 'completed'}
        aria-label={todo.status === 'completed' ? t('reopen', { title: todo.title }) : t('complete', { title: todo.title })}
        onCheckedChange={() => toggle(todo)}
        className="mt-1"
      />
      {editing?.id === todo.id ? (
        <form className="flex flex-1 gap-1" onSubmit={(e) => { e.preventDefault(); saveEdit(); }}>
          <Input aria-label={t('editLabel')} maxLength={200} autoFocus value={editing.title} onChange={(e) => setEditing({ id: todo.id, title: e.target.value })} />
          <Button type="submit" size="sm">{t('save')}</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>{t('cancel')}</Button>
        </form>
      ) : (
        <>
          <div className="min-w-0 flex-1">
            <p className={`text-sm ${todo.status === 'completed' ? 'text-muted-foreground line-through' : ''}`}>
              {todo.emoji ? <span aria-hidden className="mr-1">{todo.emoji}</span> : null}{todo.title}
            </p>
            {todo.description ? <p className="text-xs text-muted-foreground">{todo.description}</p> : null}
          </div>
          <Button type="button" variant="ghost" size="icon" aria-label={t('edit', { title: todo.title })} onClick={() => setEditing({ id: todo.id, title: todo.title })}>
            <PencilIcon size={14} aria-hidden />
          </Button>
          <Button type="button" variant="ghost" size="icon" aria-label={t('remove', { title: todo.title })} onClick={() => remove(todo)}>
            <Trash2Icon size={14} aria-hidden />
          </Button>
        </>
      )}
    </li>
  );
}

/** Loading, load-error (with retry), mutation-error and empty feedback above the list. */
function TodosStatus({ query, failed, count }: { query: { isPending: boolean; isError: boolean; isSuccess: boolean; refetch: () => unknown }; failed: boolean; count: number }) {
  const t = useTranslations('todos');
  return (
    <>
      {query.isPending && <p className="text-sm text-muted-foreground">{t('loading')}</p>}
      {query.isError && (
        <p role="alert" className="text-sm text-destructive">
          {t('loadFailed')} <Button type="button" variant="link" onClick={() => void query.refetch()}>{t('retry')}</Button>
        </p>
      )}
      {failed && <p role="alert" className="text-sm text-destructive">{t('mutateFailed')}</p>}
      {query.isSuccess && count === 0 && <p className="text-sm text-muted-foreground">{t('empty')}</p>}
    </>
  );
}

/** Add form at the bottom of the open panel; disabled at the todo limit. */
function AddTodoForm({ actions, full }: { actions: ReturnType<typeof useTodoActions>; full: boolean }) {
  const t = useTranslations('todos');
  const { draft, setDraft, add } = actions;
  return (
    <form className="flex gap-1" onSubmit={(e) => { e.preventDefault(); add(); }}>
      <Input aria-label={t('addLabel')} placeholder={t('addPlaceholder')} maxLength={200} value={draft} disabled={full} onChange={(e) => setDraft(e.target.value)} />
      <Button type="submit" size="sm" disabled={!draft.trim() || full}>
        <PlusIcon size={14} aria-hidden /> {t('add')}
      </Button>
    </form>
  );
}

/** Open panel body: status, the todo list, the add form and the limit note. */
function TodosBody({ query, todos, actions }: { query: Parameters<typeof TodosStatus>[0]['query']; todos: ChatTodo[]; actions: ReturnType<typeof useTodoActions> }) {
  const t = useTranslations('todos');
  const full = todos.length >= MAX_TODOS;
  return (
    <div className="space-y-2 border-t border-border px-3 py-2">
      <TodosStatus query={query} failed={actions.failed} count={todos.length} />
      <ul className="space-y-1">
        {todos.map((todo) => <TodoItem key={todo.id} todo={todo} actions={actions} />)}
      </ul>
      <AddTodoForm actions={actions} full={full} />
      {full && <p className="text-xs text-muted-foreground">{t('limit', { max: MAX_TODOS })}</p>}
    </div>
  );
}

/**
 * Collapsible conversation task list with optimistic add, edit, complete and delete.
 *
 * Every change updates the cache immediately and is rolled back with an error message when the
 * request fails. The server stays the source of truth: successful replies replace the cache, and the
 * list refetches whenever an assistant response finishes streaming.
 *
 * @param props - Conversation id and streaming flag.
 * @returns The card, or null without a conversation.
 */
export function TodosPanel({ conversationId, isStreaming = false }: TodosPanelProps) {
  const t = useTranslations('todos');
  const [open, setOpen] = React.useState(false);
  const query = useQuery({ queryKey: todosKey(conversationId ?? ''), queryFn: () => listTodos(conversationId!), enabled: Boolean(conversationId) });
  const { refetch } = query;
  const wasStreaming = React.useRef(isStreaming);
  const todos = React.useMemo(() => query.data ?? [], [query.data]);
  const actions = useTodoActions(conversationId ?? '', todos);

  React.useEffect(() => {
    // Assistant turn ended: pick up todos written by the manage_todos tool.
    if (wasStreaming.current && !isStreaming && conversationId) void refetch();
    wasStreaming.current = isStreaming;
  }, [isStreaming, conversationId, refetch]);

  if (!conversationId) return null;
  const done = todos.filter((todo) => todo.status === 'completed').length;

  return (
    <section aria-label={t('title')} className="mx-3 mb-2 rounded-lg border border-border bg-surface text-foreground">
      <div className="flex items-center justify-between gap-2 px-3 py-1.5">
        <span className="text-sm font-medium">
          {t('title')} <span className="text-xs font-normal text-muted-foreground">{todos.length ? t('count', { done, total: todos.length }) : ''}</span>
        </span>
        <Button type="button" variant="ghost" size="icon" aria-expanded={open} aria-label={open ? t('collapse') : t('expand')} onClick={() => setOpen((v) => !v)}>
          {open ? <ChevronUpIcon size={16} aria-hidden /> : <ChevronDownIcon size={16} aria-hidden />}
        </Button>
      </div>
      {open && <TodosBody query={query} todos={todos} actions={actions} />}
    </section>
  );
}
