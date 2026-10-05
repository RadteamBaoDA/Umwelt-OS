/** Types and contracts for strategic goal management and plan proposal acceptance. */

import type { Task, TaskStatus } from '@/modules/tasks/types';

/** Lifecycle state supported by owner goal reads and writes. */
export type GoalStatus = 'active' | 'completed' | 'paused' | 'cancelled';

/** Stable goal milestone projection; linked completion is derived from the owning task. */
export type Milestone = {
  id: string;
  title: string;
  completed: boolean;
  due_date: string | null;
  order: number;
  task_id: string | null;
};

/** Owner goal projection with progress mode, milestone state, and optimistic revision. */
export type Goal = {
  id: string;
  owner_id: number;
  title: string;
  description: string | null;
  desired_outcome: string | null;
  deadline: string | null;
  progress: number;
  manual_progress: boolean;
  status: GoalStatus;
  milestones: Milestone[];
  entity_ids: string[];
  revision: number;
  created_at: string;
  updated_at: string;
};

/** Create payload for a strategic goal and its initial unlinked milestones. */
export type GoalCreate = {
  title: string;
  description?: string | null;
  desired_outcome?: string | null;
  deadline?: string | null;
  progress?: number;
  manual_progress?: boolean;
  status?: GoalStatus;
  milestones?: Omit<Milestone, 'id'>[];
  entity_ids?: string[];
};

/** Revision-guarded goal patch; linked milestone completion is read-only to callers. */
export type GoalUpdate = {
  title?: string | null;
  description?: string | null;
  desired_outcome?: string | null;
  deadline?: string | null;
  progress?: number | null;
  manual_progress?: boolean | null;
  status?: GoalStatus | null;
  milestones?: Milestone[] | null;
  entity_ids?: string[] | null;
  expected_revision: number;
};

/** Proposed task fields that are materialized only after owner acceptance. */
export type TaskProposal = {
  title: string;
  description?: string | null;
  status?: TaskStatus;
  due_date?: string | null;
  due_at?: string | null;
  entity_ids?: string[];
};

/** Proposed stable milestone data with an optional task index, never a task identifier. */
export type ProposalMilestone = {
  id?: string | null;
  title: string;
  due_date?: string | null;
  order?: number;
  task_index?: number | null;
};

/** Stable owner review payload; contains no task IDs and requires the expected goal revision. */
export type PlanProposal = {
  proposal_id: string;
  expected_revision: number;
  milestones: ProposalMilestone[];
  tasks: TaskProposal[];
};

/** Immutable owner acceptance receipt, including created IDs and linked task IDs now missing. */
export type PlanAcceptanceResult = {
  goal: Goal;
  created_tasks: Task[];
  already_accepted: boolean;
  deleted_task_ids: string[];
  accepted_goal_revision: number;
  accepted_milestone_ids: string[];
};

/** Bounded direct-ID read outcome distinguishing owner-missing records from request failures. */
export type GoalLookupResult = { id: string; goal: Goal | null; missing: boolean };

/** Bounded owner goal query filters, including cursor continuation. */
export type GoalFilter = {
  status?: GoalStatus;
  q?: string;
  limit?: number;
  cursor?: string;
};

/** One paginated owner goal page; a null cursor marks exhaustion. */
export type GoalPage = {
  items: Goal[];
  next_cursor: string | null;
  total?: number | null;
};
