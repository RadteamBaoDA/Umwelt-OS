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

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'low-power' });
    } catch {
      propsRef.current.onUnavailable();
      return;
    }

    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-3.8, 3.8, 2.5, -2.5, 0.1, 20);
    camera.position.z = 8;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.domElement.style.display = 'block';
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    host.appendChild(renderer.domElement);

    let palette = getPalette();
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
    const resources: Array<THREE.BufferGeometry | THREE.Material> = [center.geometry, center.material, ...orbits.flatMap((orbit) => [orbit.geometry, orbit.material])];

    for (const id of Object.keys(SOURCE_POINTS) as LandingSourceId[]) {
      const route = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([SOURCE_POINTS[id], CENTER_POINT]),
        new THREE.LineBasicMaterial({ color: palette.line, transparent: true, opacity: 0.35 }),
      );
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(id === 'feed' ? 0.17 : 0.15, 20, 16),
        new THREE.MeshBasicMaterial({ color: palette.muted }),
      );
      const pulse = new THREE.Mesh(
        new THREE.SphereGeometry(0.07, 12, 10),
        new THREE.MeshBasicMaterial({ color: palette.accent }),
      );
      route.position.z = -0.1;
      mesh.position.copy(SOURCE_POINTS[id]);
      pulse.visible = false;
      scene.add(route, mesh, pulse);
      routes[id] = route;
      meshes[id] = mesh;
      pulses[id] = pulse;
      resources.push(route.geometry, route.material, mesh.geometry, mesh.material, pulse.geometry, pulse.material);
    }

    let animationFrame = 0;
    let stopPulseAt = 0;
    let width = 0;
    let height = 0;
    let visible = false;
    let didDraw = false;

    /** Resizes the fixed illustration and draws only when visible. */
    function draw(): void {
      if (!visible || width <= 0 || height <= 0) return;
      palette = getPalette();
      renderer.setSize(width, height, false);
      const ratio = width / height;
      camera.left = -2.5 * ratio;
      camera.right = 2.5 * ratio;
      camera.position.x = pointerRef.current.x * 0.18;
      camera.position.y = pointerRef.current.y * 0.12;
      camera.lookAt(0, 0, 0);
      camera.updateProjectionMatrix();
      const state = propsRef.current;
      const active = new Set(state.activeSources);
      for (const id of Object.keys(SOURCE_POINTS) as LandingSourceId[]) {
        (meshes[id].material as THREE.MeshBasicMaterial).color.set(active.has(id) ? palette.accent : palette.muted);
        (routes[id].material as THREE.LineBasicMaterial).color.set(active.has(id) ? palette.accent : palette.line);
        (routes[id].material as THREE.LineBasicMaterial).opacity = active.has(id) ? 0.78 : 0.28;
        pulses[id].visible = false;
      }
      (center.material as THREE.MeshBasicMaterial).color.set(palette.accent);
      for (const orbit of orbits) orbit.material.color.set(palette.accent);
      for (const marker of Object.values(pulses)) (marker.material as THREE.MeshBasicMaterial).color.set(palette.accent);
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
      const from = SOURCE_POINTS[id];
      cancelPulse();
      stopPulseAt = performance.now() + 700;
      const animate = (now: number) => {
        const progress = Math.min(1, Math.max(0, (700 - (stopPulseAt - now)) / 700));
        const marker = pulses[id];
        marker.visible = true;
        marker.position.lerpVectors(from, CENTER_POINT, progress);
        renderer.render(scene, camera);
        if (now < stopPulseAt) animationFrame = requestAnimationFrame(animate);
        else {
          marker.visible = false;
          renderer.render(scene, camera);
          animationFrame = 0;
        }
      };
      animationFrame = requestAnimationFrame(animate);
    }

    redrawRef.current = draw;
    pulseRef.current = pulse;
    cancelPulseRef.current = cancelPulse;
    const resize = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (!box) return;
      width = box.width;
      height = box.height;
      draw();
    });
    resize.observe(host);
    const visibility = new IntersectionObserver(([entry]) => {
      visible = Boolean(entry?.isIntersecting);
      draw();
      if (!visible && animationFrame) {
        cancelAnimationFrame(animationFrame);
        animationFrame = 0;
      }
    });
    visibility.observe(host);
    const themeChanges = new MutationObserver(draw);
    themeChanges.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-accent'] });
    const contextLost = () => propsRef.current.onUnavailable();
    renderer.domElement.addEventListener('webglcontextlost', contextLost);
    draw();

    return () => {
      redrawRef.current = null;
      pulseRef.current = null;
      cancelPulseRef.current = null;
      resize.disconnect();
      visibility.disconnect();
      themeChanges.disconnect();
      renderer.domElement.removeEventListener('webglcontextlost', contextLost);
      if (animationFrame) cancelAnimationFrame(animationFrame);
      for (const resource of resources) resource.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);

  useEffect(() => {
    redrawRef.current?.();
    if (reducedMotion) cancelPulseRef.current?.();
    if (pulseSource) pulseRef.current?.(pulseSource);
  }, [activeSources, pulseSource, reducedMotion]);

  return <div ref={hostRef} onPointerMove={trackPointer} onPointerLeave={resetPointer} className="landing-source-canvas pointer-events-auto absolute inset-0" aria-hidden="true" data-reduced-motion={reducedMotion} />;
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
