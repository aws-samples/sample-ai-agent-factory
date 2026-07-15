/**
 * AnimatedHeroBackground - Cinematic gradient background for login/landing.
 * MotionSites design language: deep base gradient + drifting aurora light
 * sources + film grain + corner accents. Used ONLY on login/landing, never
 * behind the editor canvas. Honors prefers-reduced-motion (renders a static
 * frame instead of animating).
 */

import { useEffect, useRef } from 'react';

interface Blob {
  x: number;
  y: number;
  r: number;
  hue: number;
  sat: number;
  light: number;
  dx: number;
  dy: number;
  phase: number;
}

export function AnimatedHeroBackground() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const updateCanvasSize = () => {
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    updateCanvasSize();

    // Drifting aurora light sources — AWS-adjacent blues/indigos + a warm ember.
    const blobs: Blob[] = [
      { x: 0.25, y: 0.30, r: 0.55, hue: 212, sat: 85, light: 42, dx: 0.6, dy: 0.4, phase: 0 },
      { x: 0.72, y: 0.28, r: 0.5, hue: 246, sat: 70, light: 38, dx: -0.5, dy: 0.5, phase: 1.7 },
      { x: 0.55, y: 0.78, r: 0.6, hue: 258, sat: 60, light: 30, dx: 0.4, dy: -0.5, phase: 3.1 },
      { x: 0.85, y: 0.72, r: 0.35, hue: 32, sat: 90, light: 46, dx: -0.35, dy: -0.3, phase: 4.4 },
    ];

    const prefersReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    let animationId = 0;
    let time = 0;

    const drawFrame = () => {
      // Deep base wash
      const base = ctx.createLinearGradient(0, 0, w, h);
      base.addColorStop(0, '#0b1220');
      base.addColorStop(0.55, '#0a1526');
      base.addColorStop(1, '#080d18');
      ctx.fillStyle = base;
      ctx.fillRect(0, 0, w, h);

      // Additive aurora blobs
      ctx.globalCompositeOperation = 'lighter';
      for (const b of blobs) {
        const cx = (b.x + Math.sin(time * b.dx + b.phase) * 0.06) * w;
        const cy = (b.y + Math.cos(time * b.dy + b.phase) * 0.06) * h;
        const rad = b.r * Math.max(w, h) * (0.9 + Math.sin(time + b.phase) * 0.08);
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
        g.addColorStop(0, `hsla(${b.hue}, ${b.sat}%, ${b.light}%, 0.55)`);
        g.addColorStop(0.4, `hsla(${b.hue}, ${b.sat}%, ${b.light}%, 0.22)`);
        g.addColorStop(1, `hsla(${b.hue}, ${b.sat}%, ${b.light}%, 0)`);
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, w, h);
      }
      ctx.globalCompositeOperation = 'source-over';

      // Vignette for focus
      const vig = ctx.createRadialGradient(w / 2, h * 0.42, 0, w / 2, h * 0.42, Math.max(w, h) * 0.75);
      vig.addColorStop(0, 'rgba(0,0,0,0)');
      vig.addColorStop(1, 'rgba(0,0,0,0.55)');
      ctx.fillStyle = vig;
      ctx.fillRect(0, 0, w, h);
    };

    if (prefersReduced) {
      drawFrame();
    } else {
      const animate = () => {
        time += 0.004;
        drawFrame();
        animationId = requestAnimationFrame(animate);
      };
      animate();
    }

    window.addEventListener('resize', updateCanvasSize);
    return () => {
      cancelAnimationFrame(animationId);
      window.removeEventListener('resize', updateCanvasSize);
    };
  }, []);

  return (
    <div className="fixed inset-0 overflow-hidden">
      <canvas ref={canvasRef} className="w-full h-full" style={{ display: 'block' }} />
      {/* Film grain overlay — subtle, adds premium texture over the gradient. */}
      <div
        className="absolute inset-0 pointer-events-none mix-blend-overlay"
        style={{
          opacity: 0.06,
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")",
        }}
      />
      {/* Corner accent squares - 7x7px white */}
      <div className="absolute top-8 left-8 w-[7px] h-[7px] bg-white/90" />
      <div className="absolute top-8 right-8 w-[7px] h-[7px] bg-white/90" />
      <div className="absolute bottom-8 left-8 w-[7px] h-[7px] bg-white/90" />
      <div className="absolute bottom-8 right-8 w-[7px] h-[7px] bg-white/90" />
    </div>
  );
}
