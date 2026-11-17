/** Types and contracts for owner task management. */

/** Task lifecycle values accepted by owner task reads and writes. */
export type TaskStatus = 'inbox' | 'todo' | 'in_progress' | 'blocked' | 'done' | 'cancelled';
/** Owner task list views with date-aware today and upcoming semantics. */
export type TaskView = 'inbox' | 'today' | 'upcoming' | 'blocked' | 'completed' | 'all';

/** Owner task projection, including optimistic revision and goal/entity references. */
export type Task = {
  id: string;
  owner_id: number;
  title: string;
  description: string | null;
  status: TaskStatus;
  due_date: string | null;
  due_at: string | null;
  completed_at: string | null;
  goal_id: string | null;
  entity_ids: string[];
  revision: number;
  created_at: string;
  updated_at: string;
};

/** Create payload for an owner task; omitted optional fields use server defaults. */
export type TaskCreate = {
  title: string;
  description?: string | null;
  status?: TaskStatus;
  due_date?: string | null;
  due_at?: string | null;
  goal_id?: string | null;
  entity_ids?: string[];
};

/** Revision-guarded partial task mutation; date-only and offset-aware due values remain exclusive. */
export type TaskUpdate = {
  title?: string | null;
  description?: string | null;
  status?: TaskStatus | null;
  due_date?: string | null;
  due_at?: string | null;
  completed_at?: string | null;
  goal_id?: string | null;
  entity_ids?: string[] | null;
  expected_revision: number;
};

/** Bounded direct-ID read outcome distinguishing owner-missing records from request failures. */
export type TaskLookupResult = { id: string; task: Task | null; missing: boolean };

/** Bounded owner task query filters; cursor continues the same stable result set. */
export type TaskFilter = {
  view?: TaskView;
  status?: TaskStatus;
  goal_id?: string;
  entity_id?: string;
  due_date_from?: string;
  due_date_to?: string;
  due_at_from?: string;
  due_at_to?: string;
  q?: string;
  timezone?: string;
  limit?: number;
  cursor?: string;
};

/** One paginated owner task page; a null cursor marks exhaustion. */
export type TaskPage = {
  items: Task[];
  next_cursor: string | null;
  total?: number | null;
};
