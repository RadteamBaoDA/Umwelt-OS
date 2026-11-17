import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DashboardPlacement, GadgetInstance } from './api';
import type { ActiveGesture } from './grid-geometry';
import { useGridProjection, type GridProjectionInputs } from './use-grid-projection';

const placement = (id: string, x: number, y: number): DashboardPlacement => ({ instance_id: id, x, y, w: 4, h: 4 }) as DashboardPlacement;
const base = (overrides: Partial<GridProjectionInputs> = {}): GridProjectionInputs => ({
  gridWidth: 1000,
  columnCount: 20,
  isMobileView: false,
  isTabletView: false,
  isEditMode: false,
  breakpoint: 'desktop',
  placements: [placement('a', 0, 0), placement('b', 4, 0)],
  instances: [] as GadgetInstance[],
  frameHeaderHeight: 0,
  gesture: null,
  ...overrides,
});
const gesture = (overrides: Partial<ActiveGesture> = {}): ActiveGesture =>
  ({
    type: 'move',
    instanceId: 'a',
    breakpoint: 'desktop',
    columnCount: 20,
    visibleGroupKey: 'g',
    startX: 0,
    startY: 0,
    currentPlacement: placement('a', 8, 8),
    ...overrides,
  }) as ActiveGesture;

describe('useGridProjection', () => {
  it('derives a square cell size from width and columns and shows stored placements', () => {
    const inputs = base();
    const { result } = renderHook(() => useGridProjection(inputs));
    expect(result.current.cellSize).toBeGreaterThan(0);
    expect(result.current.renderColumns).toBe(20);
    expect(result.current.renderCell).toBe(result.current.cellSize);
    expect(result.current.displayPlacements).toBe(inputs.placements);
    expect(result.current.projectionPending).toBe(false);
  });

  it('has zero cell size for an unusable grid width', () => {
    const { result } = renderHook(() => useGridProjection(base({ gridWidth: Number.NaN })));
    expect(result.current.cellSize).toBe(0);
  });

  it('overlays the active gesture placement only for the same breakpoint and column count', () => {
    const matching = renderHook(() => useGridProjection(base({ gesture: gesture() }))).result.current.displayPlacements;
    expect(matching[0]).toMatchObject({ instance_id: 'a', x: 8, y: 8 });
    expect(matching[1]).toMatchObject({ instance_id: 'b', x: 4 });
    for (const stale of [gesture({ columnCount: 12 }), gesture({ breakpoint: 'mobile' })]) {
      expect(renderHook(() => useGridProjection(base({ gesture: stale }))).result.current.displayPlacements[0]).toMatchObject({
        x: 0,
        y: 0,
      });
    }
  });

  it('reports grid metrics only once the frame header height is measured', () => {
    const onGridMetricsChange = vi.fn();
    const inputs = base({ onGridMetricsChange });
    const { rerender } = renderHook((props) => useGridProjection(props), { initialProps: inputs });
    expect(onGridMetricsChange).toHaveBeenLastCalledWith(null);
    rerender({ ...inputs, frameHeaderHeight: 32 });
    expect(onGridMetricsChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ frameHeaderHeight: 32, cellSize: expect.any(Number), stride: expect.any(Number) }),
    );
  });

  it('mobile view waits for measurement before projecting and clears the projection callback elsewhere', () => {
    const onReadableProjectionChange = vi.fn();
    const mobile = renderHook(() => useGridProjection(base({ isMobileView: true, columnCount: 4, onReadableProjectionChange })));
    expect(mobile.result.current.projectionPending).toBe(true);
    expect(onReadableProjectionChange).toHaveBeenCalled();
    const desktopCallback = vi.fn();
    renderHook(() => useGridProjection(base({ onReadableProjectionChange: desktopCallback })));
    expect(desktopCallback).toHaveBeenCalledWith(null);
  });

  it('tablet view-only layout renders eight columns once measured; editing keeps the desktop geometry', () => {
    const instances = ['a', 'b'].map((id) => ({ id, definition: { renderer: 'metric' } }) as unknown as GadgetInstance);
    const measured = { isTabletView: true, frameHeaderHeight: 32, instances };
    const tablet = renderHook(() => useGridProjection(base(measured))).result.current;
    expect(tablet.renderColumns).toBe(8);
    expect(tablet.renderCell).toBeGreaterThan(tablet.cellSize);
    expect(renderHook(() => useGridProjection(base({ ...measured, isEditMode: true }))).result.current.renderColumns).toBe(20);
  });
});
