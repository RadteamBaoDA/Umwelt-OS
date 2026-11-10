'use client';

import { useEffect, useRef } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import * as THREE from 'three';

export type LandingSourceId = 'website' | 'feed' | 'document';

type SourceCanvasProps = {
  activeSources: LandingSourceId[];
  pulseSource: LandingSourceId | null;
  reducedMotion: boolean;
  onReady: () => void;
  onUnavailable: () => void;
};

const SOURCE_POINTS: Record<LandingSourceId, THREE.Vector3> = {
  website: new THREE.Vector3(-2.7, 1.1, 0.1),
  feed: new THREE.Vector3(-2.7, 0, -0.3),
  document: new THREE.Vector3(-2.7, -1.1, 0.1),
};
const CENTER_POINT = new THREE.Vector3(0, 0, 0);

/** Three.js objects of the source illustration plus every geometry/material that must be disposed. */
type SceneParts = ReturnType<typeof buildScene>;

/** Builds the orthographic scene (workspace core, orbits, per-source marker, route and pulse) in the given palette. */
function buildScene(palette: ReturnType<typeof getPalette>) {
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-3.8, 3.8, 2.5, -2.5, 0.1, 20);
  camera.position.z = 8;

  const center = new THREE.Mesh(
    new THREE.IcosahedronGeometry(0.68, 2),
    new THREE.MeshBasicMaterial({ color: palette.accent, wireframe: true, transparent: true, opacity: 0.7 }),
  );
  center.position.copy(CENTER_POINT);
  scene.add(center);
  // Two tilted orbits make the fixed scene read as a volume without continuous motion.
  const orbits = [0.25, -0.7].map((angle) => {
    const orbit = new THREE.Mesh(
      new THREE.TorusGeometry(0.95, 0.012, 8, 80),
      new THREE.MeshBasicMaterial({ color: palette.accent, transparent: true, opacity: 0.45 }),
    );
    orbit.rotation.set(1.05, angle, angle);
    scene.add(orbit);
    return orbit;
  });

  const meshes = {} as Record<LandingSourceId, THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>>;
  const routes = {} as Record<LandingSourceId, THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>>;
  const pulses = {} as Record<LandingSourceId, THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>>;
  const resources: Array<THREE.BufferGeometry | THREE.Material> = [
    center.geometry,
    center.material,
    ...orbits.flatMap((orbit) => [orbit.geometry, orbit.material]),
  ];

  for (const id of Object.keys(SOURCE_POINTS) as LandingSourceId[]) {
    const route = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([SOURCE_POINTS[id], CENTER_POINT]),
      new THREE.LineBasicMaterial({ color: palette.line, transparent: true, opacity: 0.35 }),
    );
    const mesh = new THREE.Mesh(
      new THREE.SphereGeometry(id === 'feed' ? 0.17 : 0.15, 20, 16),
      new THREE.MeshBasicMaterial({ color: palette.muted }),
    );
    const pulse = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 10), new THREE.MeshBasicMaterial({ color: palette.accent }));
    route.position.z = -0.1;
    mesh.position.copy(SOURCE_POINTS[id]);
    pulse.visible = false;
    scene.add(route, mesh, pulse);
    routes[id] = route;
    meshes[id] = mesh;
    pulses[id] = pulse;
    resources.push(route.geometry, route.material, mesh.geometry, mesh.material, pulse.geometry, pulse.material);
  }
  return { scene, camera, center, orbits, meshes, routes, pulses, resources };
}

/** Recolors the scene for the current palette and marks the active sources; hides any in-flight pulse markers. */
function paintScene(
  { meshes, routes, pulses, center, orbits }: SceneParts,
  active: Set<LandingSourceId>,
  palette: ReturnType<typeof getPalette>,
): void {
  for (const id of Object.keys(SOURCE_POINTS) as LandingSourceId[]) {
    (meshes[id].material as THREE.MeshBasicMaterial).color.set(active.has(id) ? palette.accent : palette.muted);
    (routes[id].material as THREE.LineBasicMaterial).color.set(active.has(id) ? palette.accent : palette.line);
    (routes[id].material as THREE.LineBasicMaterial).opacity = active.has(id) ? 0.78 : 0.28;
    pulses[id].visible = false;
  }
  (center.material as THREE.MeshBasicMaterial).color.set(palette.accent);
  for (const orbit of orbits) orbit.material.color.set(palette.accent);
  for (const marker of Object.values(pulses)) (marker.material as THREE.MeshBasicMaterial).color.set(palette.accent);
}

/** Observes size, visibility, theme class changes and context loss on the host; returns the disconnect function. */
function observeHost(
  host: HTMLElement,
  canvas: HTMLCanvasElement,
  on: { resize: (width: number, height: number) => void; visible: (visible: boolean) => void; theme: () => void; lost: () => void },
): () => void {
  const resize = new ResizeObserver((entries) => {
    const box = entries[0]?.contentRect;
    if (box) on.resize(box.width, box.height);
  });
  resize.observe(host);
  const visibility = new IntersectionObserver(([entry]) => on.visible(Boolean(entry?.isIntersecting)));
  visibility.observe(host);
  const themeChanges = new MutationObserver(on.theme);
  themeChanges.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-accent'] });
  canvas.addEventListener('webglcontextlost', on.lost);
  return () => {
    resize.disconnect();
    visibility.disconnect();
    themeChanges.disconnect();
    canvas.removeEventListener('webglcontextlost', on.lost);
  };
}

/** Creates the transparent low-power WebGL renderer inside `host`, or null when WebGL is unavailable. */
function createRenderer(host: HTMLElement): THREE.WebGLRenderer | null {
  let renderer: THREE.WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'low-power' });
  } catch {
    return null;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.domElement.style.display = 'block';
  renderer.domElement.style.width = '100%';
  renderer.domElement.style.height = '100%';
  host.appendChild(renderer.domElement);
  return renderer;
}

/** Fits the orthographic frustum to the aspect ratio and nudges the camera toward the pointer (-1..1 on each axis). */
function aimCamera(camera: THREE.OrthographicCamera, ratio: number, pointer: { x: number; y: number }): void {
  camera.left = -2.5 * ratio;
  camera.right = 2.5 * ratio;
  camera.position.x = pointer.x * 0.18;
  camera.position.y = pointer.y * 0.12;
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();
}

/**
 * Animates the pulse marker for `id` from its source to the workspace core until `stopAt`.
 * `onFrame` receives each pending animation-frame handle (0 once finished) so the caller can cancel it.
 */
function startPulse(
  { scene, camera, pulses }: SceneParts,
  renderer: THREE.WebGLRenderer,
  id: LandingSourceId,
  stopAt: number,
  onFrame: (frame: number) => void,
): void {
  const marker = pulses[id];
  const animate = (now: number) => {
    const progress = Math.min(1, Math.max(0, (700 - (stopAt - now)) / 700));
    marker.visible = true;
    marker.position.lerpVectors(SOURCE_POINTS[id], CENTER_POINT, progress);
    renderer.render(scene, camera);
    if (now < stopAt) onFrame(requestAnimationFrame(animate));
    else {
      marker.visible = false;
      renderer.render(scene, camera);
      onFrame(0);
    }
  };
  onFrame(requestAnimationFrame(animate));
}

/** Mutable handles shared between {@link SourceCanvas} and the mounted WebGL scene. */
type SceneContext = {
  propsRef: { current: Pick<SourceCanvasProps, 'activeSources' | 'reducedMotion' | 'onReady' | 'onUnavailable'> };
  pointerRef: { current: { x: number; y: number } };
  redrawRef: { current: (() => void) | null };
  pulseRef: { current: ((id: LandingSourceId) => void) | null };
  cancelPulseRef: { current: (() => void) | null };
};

/**
 * Creates the renderer and scene inside `host`, publishes redraw/pulse/cancel handles on the context refs and
 * starts the observers. Returns the teardown (disposes GPU resources, detaches the canvas and clears the handles),
 * or undefined after reporting `onUnavailable` when WebGL cannot start.
 */
function mountSourceScene(
  host: HTMLElement,
  { propsRef, pointerRef, redrawRef, pulseRef, cancelPulseRef }: SceneContext,
): (() => void) | undefined {
  const created = createRenderer(host);
  if (!created) {
    propsRef.current.onUnavailable();
    return undefined;
  }
  const renderer: THREE.WebGLRenderer = created;
  let palette = getPalette();
  const parts = buildScene(palette);
  const { scene, camera, pulses, resources } = parts;
  let animationFrame = 0;
  let stopPulseAt = 0;
  let [width, height, visible, didDraw] = [0, 0, false, false];

  /** Resizes the fixed illustration and draws only when visible. */
  function draw(): void {
    if (!visible || width <= 0 || height <= 0) return;
    palette = getPalette();
    renderer.setSize(width, height, false);
    aimCamera(camera, width / height, pointerRef.current);
    paintScene(parts, new Set(propsRef.current.activeSources), palette);
    renderer.render(scene, camera);
    if (!didDraw) {
      didDraw = true;
      propsRef.current.onReady();
    }
  }

  /** Stops the transient pulse and restores the static source map. */
  function cancelPulse(): void {
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    for (const marker of Object.values(pulses)) marker.visible = false;
    draw();
  }

  /** Runs a brief source-to-workspace pulse after an explicit source selection. */
  function pulse(id: LandingSourceId): void {
    if (propsRef.current.reducedMotion || !visible) return;
    cancelPulse();
    stopPulseAt = performance.now() + 700;
    startPulse(parts, renderer, id, stopPulseAt, (frame) => {
      animationFrame = frame;
    });
  }

  redrawRef.current = draw;
  pulseRef.current = pulse;
  cancelPulseRef.current = cancelPulse;
  const stopObserving = observeHost(host, renderer.domElement, {
    resize: (w, h) => {
      width = w;
      height = h;
      draw();
    },
    visible: (isVisible) => {
      visible = isVisible;
      draw();
      if (!visible && animationFrame) {
        cancelAnimationFrame(animationFrame);
        animationFrame = 0;
      }
    },
    theme: draw,
    lost: () => propsRef.current.onUnavailable(),
  });
  draw();

  return () => {
    redrawRef.current = null;
    pulseRef.current = null;
    cancelPulseRef.current = null;
    stopObserving();
    if (animationFrame) cancelAnimationFrame(animationFrame);
    for (const resource of resources) resource.dispose();
    renderer.dispose();
    renderer.domElement.remove();
  };
}

/** Draws the source-to-workspace illustration in one disposable WebGL context. */
export function SourceCanvas({ activeSources, pulseSource, reducedMotion, onReady, onUnavailable }: SourceCanvasProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const redrawRef = useRef<(() => void) | null>(null);
  const pulseRef = useRef<((id: LandingSourceId) => void) | null>(null);
  const cancelPulseRef = useRef<(() => void) | null>(null);
  const propsRef = useRef({ activeSources, reducedMotion, onReady, onUnavailable });
  const pointerRef = useRef({ x: 0, y: 0 });

  useEffect(() => {
    propsRef.current = { activeSources, reducedMotion, onReady, onUnavailable };
    redrawRef.current?.();
  }, [activeSources, pulseSource, reducedMotion, onReady, onUnavailable]);

  /** Adds a subtle camera response for mouse input; touch and reduced-motion users keep a fixed scene. */
  function trackPointer(event: ReactPointerEvent<HTMLDivElement>): void {
    if (event.pointerType === 'touch' || propsRef.current.reducedMotion) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    pointerRef.current = {
      x: ((event.clientX - bounds.left) / bounds.width - 0.5) * 2,
      y: (0.5 - (event.clientY - bounds.top) / bounds.height) * 2,
    };
    redrawRef.current?.();
  }

  /** Returns the illustration to its fixed camera when the pointer leaves. */
  function resetPointer(): void {
    pointerRef.current = { x: 0, y: 0 };
    redrawRef.current?.();
  }

  useEffect(() => {
    const host = hostRef.current;
    if (!host || typeof window === 'undefined') return;
    return mountSourceScene(host, { propsRef, pointerRef, redrawRef, pulseRef, cancelPulseRef });
  }, []);

  useEffect(() => {
    redrawRef.current?.();
    if (reducedMotion) cancelPulseRef.current?.();
    if (pulseSource) pulseRef.current?.(pulseSource);
  }, [activeSources, pulseSource, reducedMotion]);

  return (
    <div
      ref={hostRef}
      onPointerMove={trackPointer}
      onPointerLeave={resetPointer}
      className="landing-source-canvas pointer-events-auto absolute inset-0"
      aria-hidden="true"
      data-reduced-motion={reducedMotion}
    />
  );
}

/** Reads the current interface palette for the one non-semantic canvas illustration. */
function getPalette(): { accent: THREE.ColorRepresentation; muted: THREE.ColorRepresentation; line: THREE.ColorRepresentation } {
  const read = (token: string) => getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return {
    accent: new THREE.Color(read('--accent')),
    muted: new THREE.Color(read('--muted')),
    line: new THREE.Color(read('--line')),
  };
}
