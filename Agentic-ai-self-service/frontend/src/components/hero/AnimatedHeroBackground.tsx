/**
 * AnimatedHeroBackground - Cinematic gradient background for login/landing.
 * MotionSites design language: animated gradient + corner accents.
 * Used ONLY on login screen, not behind the editor canvas.
 */

import { useEffect, useRef } from 'react';

export function AnimatedHeroBackground() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const updateCanvasSize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    };
    updateCanvasSize();

    let animationId: number;
    let time = 0;

    const animate = () => {
      time += 0.005;

      // Create animated gradient
      const gradient = ctx.createLinearGradient(
        0,
        0,
        canvas.width,
        canvas.height
      );

      const hue1 = (200 + Math.sin(time) * 20) % 360;
      const hue2 = (240 + Math.cos(time * 0.8) * 20) % 360;

      gradient.addColorStop(0, `hsl(${hue1}, 70%, 20%)`);
      gradient.addColorStop(0.5, `hsl(${hue2}, 60%, 15%)`);
      gradient.addColorStop(1, 'hsl(220, 50%, 10%)');

      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      animationId = requestAnimationFrame(animate);
    };

    animate();

    window.addEventListener('resize', updateCanvasSize);

    return () => {
      cancelAnimationFrame(animationId);
      window.removeEventListener('resize', updateCanvasSize);
    };
  }, []);

  return (
    <div className="fixed inset-0 overflow-hidden">
      <canvas
        ref={canvasRef}
        className="w-full h-full"
        style={{ display: 'block' }}
      />
      {/* Corner accent squares - 7x7px white */}
      <div className="absolute top-8 left-8 w-[7px] h-[7px] bg-white" />
      <div className="absolute top-8 right-8 w-[7px] h-[7px] bg-white" />
      <div className="absolute bottom-8 left-8 w-[7px] h-[7px] bg-white" />
      <div className="absolute bottom-8 right-8 w-[7px] h-[7px] bg-white" />
    </div>
  );
}
