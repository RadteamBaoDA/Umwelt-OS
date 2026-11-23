import type { ReactNode } from 'react';

/** Test double for the News StoryList: renders one row per story and the injected row action. */
export function StoryListProbe({
  stories,
  renderStoryAction,
}: {
  stories: Array<Record<string, unknown>>;
  renderStoryAction?: (story: never) => ReactNode;
}) {
  return (
    <ul>
      {stories.map((story) => (
        <li key={String(story.id)}>
          <span>{String(story.title)}</span>
          {renderStoryAction?.(story as never)}
        </li>
      ))}
    </ul>
  );
}
