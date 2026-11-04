import { act, fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardMessages } from '@/core/messages/dashboard';
import type { DashboardPlacement, GadgetInstance } from './api';

vi.mock('./widget-registry', async (original) => ({
  ...(await original<typeof import('./widget-registry')>()),
  RegisteredGadget: () => null,
}));
/** Frame stub: a focusable region with a drag handle that forwards pointer-down. */
vi.mock('./gadget-frame', () => ({
  GadgetFrame: (props: { instance: GadgetInstance; isDragging: boolean; isResizing: boolean; onDragHandlePointerDown: (e: React.PointerEvent) => void; onHeaderMeasured: (h: number) => void }) => (
    <div data-testid={`frame-${props.instance.id}`} tabIndex={0} data-dragging={props.isDragging} data-resizing={props.isResizing}>
      <button type="button" data-testid={`drag-${props.instance.id}`} onPointerDown={props.onDragHandlePointerDown} onClick={() => props.onHeaderMeasured(40)}>drag</button>
    </div>
  ),
}));

import { DashboardGrid, doPlacementsOverlap, resolveCollisions } from './dashboard-grid';
import { isGestureStale, nudgePlacement, placementForPointer, type ActiveGesture } from './grid-geometry';

const rect = (id: string, x: number, y: number, w = 4, h = 4): DashboardPlacement => ({ instance_id: id, x, y, w, h });
const instance = (id: string) => ({ id, group_id: 'g', definition_id: id, title: null, position: 0, definition: { renderer: 'news_feed', source_ids: [] } }) as unknown as GadgetInstance;

describe('grid geometry helpers', () => {
  it('detects overlap on a half-open basis', () => {
    expect(doPlacementsOverlap(rect('a', 0, 0), rect('b', 4, 0))).toBe(false);
    expect(doPlacementsOverlap(rect('a', 0, 0), rect('b', 3, 3))).toBe(true);
  });

  it('pushes overlapped gadgets down, giving the active one right of way', () => {
    const resolved = resolveCollisions([rect('a', 0, 0), rect('b', 0, 0)], 'a', 20, () => ({ minW: 4, minH: 4 }));
    expect(resolved).toEqual([rect('a', 0, 0), rect('b', 0, 4)]);
    expect(resolveCollisions([rect('a', 18, 0)], 'a', 20, () => ({ minW: 4, minH: 4 }))).toBeNull();
  });

  it('nudges by one unit and refuses moves outside the grid or below the minimum', () => {
    const min = { minW: 4, minH: 4 };
    expect(nudgePlacement(rect('a', 0, 0), 'ArrowRight', false, 20, min)).toMatchObject({ x: 1 });
    expect(nudgePlacement(rect('a', 0, 0), 'ArrowLeft', false, 20, min)).toBeNull();
    expect(nudgePlacement(rect('a', 16, 0), 'ArrowRight', false, 20, min)).toBeNull();
    expect(nudgePlacement(rect('a', 0, 0), 'ArrowDown', true, 20, min)).toMatchObject({ h: 5 });
    expect(nudgePlacement(rect('a', 0, 0), 'ArrowUp', true, 20, min)).toBeNull();
    expect(nudgePlacement(rect('a', 0, 0), 'a', false, 20, min)).toBeNull();
  });

  it('turns pointer deltas into clamped move and resize rectangles', () => {
    const base: ActiveGesture = {
      type: 'move', instanceId: 'a', breakpoint: 'desktop', columnCount: 20, visibleGroupKey: 'k', startX: 100, startY: 100, cellStride: 50,
      initialPlacement: rect('a', 2, 2), currentPlacement: rect('a', 2, 2), backupPlacements: [], minW: 4, minH: 4,
    };
    expect(placementForPointer(base, 200, 300, 20)).toEqual({ x: 4, y: 6 });
    expect(placementForPointer(base, -1000, -1000, 20)).toEqual({ x: 0, y: 0 });
    const resize = { ...base, type: 'resize' as const, direction: 'se' as const };
    expect(placementForPointer(resize, 100 + 50 * 100, 300, 20)).toEqual({ w: 18, h: 8 });
    expect(placementForPointer(resize, -1000, -1000, 20)).toEqual({ w: 4, h: 4 });
  });

  it('marks a gesture stale when editing stops or the owning layout changes', () => {
    const gesture = { breakpoint: 'desktop', columnCount: 20, visibleGroupKey: 'k' } as ActiveGesture;
    const live = { breakpoint: 'desktop' as const, requestedBreakpoint: 'desktop' as const, columnCount: 20, visibleGroupKey: 'k', interactionEnabled: true };
    expect(isGestureStale(gesture, live)).toBe(false);
    expect(isGestureStale(gesture, { ...live, interactionEnabled: false })).toBe(true);
    expect(isGestureStale(gesture, { ...live, requestedBreakpoint: 'mobile' })).toBe(true);
    expect(isGestureStale(gesture, { ...live, columnCount: 8 })).toBe(true);
    expect(isGestureStale(gesture, { ...live, visibleGroupKey: 'other' })).toBe(true);
  });
});

describe('DashboardGrid interactions', () => {
  beforeEach(() => {
    // 20 columns of 1px gaps => 12px gap; width 1000 gives a stride the test can reason about.
    Element.prototype.getBoundingClientRect = () => ({ width: 1000, height: 600, top: 0, left: 0, right: 1000, bottom: 600, x: 0, y: 0, toJSON: () => ({}) });
  });

  /** Renders the grid in edit mode with two gadgets. */
  const renderGrid = (onLayoutChange = vi.fn()) => {
    const placements = [rect('a', 0, 0), rect('b', 4, 0)];
    render(
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
        <DashboardGrid instances={[instance('a'), instance('b')]} placements={placements} visibleInstanceIds={['a', 'b']} breakpoint="desktop" isEditMode onLayoutChange={onLayoutChange} />
      </NextIntlClientProvider>,
    );
    return onLayoutChange;
  };

  it('commits a keyboard move with collision resolution and announces the position', () => {
    const onLayoutChange = renderGrid();
    fireEvent.keyDown(screen.getByTestId('frame-a'), { key: 'ArrowRight' });
    expect(onLayoutChange).toHaveBeenCalledWith([rect('a', 1, 0), rect('b', 4, 4)]);
    expect(screen.getAllByRole('status')[0]).toHaveTextContent(dashboardMessages['en-us'].positionLabel.replace('{x}', '2').replace('{y}', '1').replace('{w}', '4').replace('{h}', '4'));
  });

  it('ignores keys that change nothing and does not commit', () => {
    const onLayoutChange = renderGrid();
    fireEvent.keyDown(screen.getByTestId('frame-a'), { key: 'ArrowLeft' });
    expect(onLayoutChange).not.toHaveBeenCalled();
  });

  it('drags a gadget with the pointer and commits on release', () => {
    const onLayoutChange = renderGrid();
    fireEvent.pointerDown(screen.getByTestId('drag-a'), { button: 0, clientX: 0, clientY: 0 });
    expect(screen.getByTestId('frame-a')).toHaveAttribute('data-dragging', 'true');
    // cell stride = (1000 - 12*19)/20 + 12 = 50.6px; 2 strides right.
    act(() => { window.dispatchEvent(new MouseEvent('pointermove', { clientX: 101, clientY: 0 })); });
    act(() => { window.dispatchEvent(new MouseEvent('pointerup')); });
    expect(onLayoutChange).toHaveBeenCalledTimes(1);
    expect(onLayoutChange.mock.calls[0][0].find((p: DashboardPlacement) => p.instance_id === 'a')).toMatchObject({ x: 2, y: 0 });
    expect(screen.getByTestId('frame-a')).toHaveAttribute('data-dragging', 'false');
  });

  it('cancels a running drag on Escape without committing', () => {
    const onLayoutChange = renderGrid();
    fireEvent.pointerDown(screen.getByTestId('drag-a'), { button: 0, clientX: 0, clientY: 0 });
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    act(() => { window.dispatchEvent(new MouseEvent('pointerup')); });
    expect(onLayoutChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('frame-a')).toHaveAttribute('data-dragging', 'false');
  });
});
