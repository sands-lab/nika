/* ── HERO CANVAS NETWORK ANIMATION ─────────────────────────────────────── */
(function () {
  const canvas = document.getElementById('hero-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  let W, H, nodes, edges, animId;

  function resize() {
    W = canvas.width  = canvas.offsetWidth;
    H = canvas.height = canvas.offsetHeight;
  }

  function randBetween(a, b) { return a + Math.random() * (b - a); }

  function init() {
    resize();
    const N = Math.min(Math.floor(W * H / 14000), 80);
    nodes = Array.from({ length: N }, () => ({
      x:  randBetween(0, W),
      y:  randBetween(0, H),
      vx: randBetween(-0.3, 0.3),
      vy: randBetween(-0.3, 0.3),
      r:  randBetween(2, 4),
    }));

    buildEdges();
  }

  function buildEdges() {
    const threshold = Math.min(W, H) * 0.22;
    edges = [];
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        if (dx * dx + dy * dy < threshold * threshold) {
          edges.push([i, j]);
        }
      }
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);

    // Update positions
    for (const n of nodes) {
      n.x += n.vx;
      n.y += n.vy;
      if (n.x < 0 || n.x > W) n.vx *= -1;
      if (n.y < 0 || n.y > H) n.vy *= -1;
    }

    // Rebuild edges periodically (every frame is fine for small N)
    const threshold = Math.min(W, H) * 0.22;
    const threshold2 = threshold * threshold;

    // Draw edges
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < threshold2) {
          const alpha = 1 - d2 / threshold2;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(0,212,255,${alpha * 0.25})`;
          ctx.lineWidth = 1;
          ctx.stroke();
        }
      }
    }

    // Draw nodes
    for (const n of nodes) {
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0,212,255,0.6)';
      ctx.fill();
    }

    animId = requestAnimationFrame(draw);
  }

  function start() {
    if (animId) cancelAnimationFrame(animId);
    init();
    draw();
  }

  window.addEventListener('resize', () => {
    if (animId) cancelAnimationFrame(animId);
    start();
  });

  start();
})();

/* ── NAVBAR SCROLL BEHAVIOUR ────────────────────────────────────────────── */
(function () {
  const navbar = document.getElementById('navbar');
  function onScroll() {
    navbar.classList.toggle('scrolled', window.scrollY > 20);
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();

/* ── MOBILE MENU ─────────────────────────────────────────────────────────── */
(function () {
  const btn   = document.getElementById('nav-hamburger');
  const links = document.getElementById('nav-links');
  if (!btn || !links) return;

  btn.addEventListener('click', () => {
    const open = links.classList.toggle('open');
    btn.setAttribute('aria-expanded', open);
  });

  // Close on link click
  links.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', () => links.classList.remove('open'));
  });

  // Close on outside click
  document.addEventListener('click', e => {
    if (!btn.contains(e.target) && !links.contains(e.target)) {
      links.classList.remove('open');
    }
  });
})();

/* ── SCROLL REVEAL ───────────────────────────────────────────────────────── */
(function () {
  const targets = document.querySelectorAll(
    '.feature-card, .scenario-card, .issue-category, .qs-step, ' +
    '.overview-card, .overview-image-frame, .mcp-server, .cite-card'
  );

  targets.forEach(el => el.classList.add('reveal'));

  const observer = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.1, rootMargin: '0px 0px -40px 0px' }
  );

  targets.forEach(el => observer.observe(el));
})();

/* ── COPY BUTTONS ────────────────────────────────────────────────────────── */
(function () {
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetId = btn.getAttribute('data-target');
      const pre = document.getElementById(targetId);
      if (!pre) return;

      const text = pre.innerText || pre.textContent;
      navigator.clipboard.writeText(text).then(() => {
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove('copied');
        }, 2000);
      });
    });
  });
})();

/* ── ACTIVE NAV LINK ON SCROLL ───────────────────────────────────────────── */
(function () {
  const sections = document.querySelectorAll('section[id]');
  const links    = document.querySelectorAll('.nav-links a[href^="#"]');

  const observer = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          links.forEach(l => l.classList.remove('active'));
          const active = document.querySelector(
            `.nav-links a[href="#${entry.target.id}"]`
          );
          if (active) active.classList.add('active');
        }
      });
    },
    { threshold: 0.4 }
  );

  sections.forEach(s => observer.observe(s));
})();
