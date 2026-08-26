// HelloDJ web-ui glue: ambient background + HTMX/Alpine helpers.
// Kept tiny per the modern-web-ui standard (HTMX + Alpine do the heavy lifting).
(function () {
  "use strict";

  const reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  // Ambient background: a slow OKLCH mesh gradient. Falls back to a static
  // gradient when the tab is hidden or the user prefers reduced motion.
  function initBackground() {
    const canvas = document.getElementById("bg-shader");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    function resize() {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener("resize", resize);

    function paint(t) {
      const w = canvas.width;
      const h = canvas.height;
      const g = ctx.createLinearGradient(0, 0, w, h);
      const shift = reduceMotion ? 0 : Math.sin(t / 6000) * 0.04;
      g.addColorStop(0, "oklch(0.14 0.03 280)");
      g.addColorStop(0.5, `oklch(${0.16 + shift} 0.06 290)`);
      g.addColorStop(1, "oklch(0.11 0.02 275)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }

    if (reduceMotion) {
      paint(0);
      return;
    }

    let last = 0;
    function loop(t) {
      if (!document.hidden && t - last > 33) {
        // ~30fps cap for a background effect.
        paint(t);
        last = t;
      }
      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
  }

  // Auto-dismiss toasts after 5s.
  function initToasts() {
    document.body.addEventListener("htmx:afterSwap", function () {
      document.querySelectorAll(".toast").forEach(function (toast) {
        if (toast.dataset.armed) return;
        toast.dataset.armed = "1";
        setTimeout(function () {
          toast.remove();
        }, 5000);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initBackground();
    initToasts();
  });
})();
