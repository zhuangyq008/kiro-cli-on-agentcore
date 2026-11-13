// Deterministic renderer: renderAt(t) draws the frame for time t (seconds) and returns a key that
// changes only when the picture changes, so render.py can skip identical frames.
(function () {
  const T = window.TIMELINE;
  const PROMPT = '<span class="pr">dev@laptop:~/demo$</span> ';
  const termCache = new Map();
  document.getElementById('architecture-diagram').innerHTML = window.ARCHITECTURE_SVG || '';

  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  const ease = (x) => 1 - Math.pow(1 - clamp01(x), 3);
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  function colorize(text) {
    return esc(text)
      .replace(/\b(SUCCEEDED|passed|pass)\b/g, '<span class="ok">$1</span>')
      .replace(/\b(FAILED|TIMED_OUT|failed)\b/g, '<span class="bad">$1</span>')
      .replace(/\b(UNDERSTAND|PLAN|EXECUTE|VERIFY|RUN)\b/g, '<span class="hl">$1</span>')
      .replace(/\btsk_[0-9A-Z]+\b/g, '<span class="id">$&</span>');
  }

  function termHtml(steps, t) {
    let html = '';
    let idle = true; // true when the last visible step has finished and a fresh prompt is waiting
    for (const st of steps) {
      if (t < st.t0) break;
      const typed = Math.min(st.cmd.length, Math.floor(((t - st.t0) / st.typeDur) * st.cmd.length));
      html += PROMPT + '<span class="cmd">' + esc(st.cmd.slice(0, typed)) + '</span>';
      if (typed < st.cmd.length) {
        idle = false;
        html += '<span class="cur"></span>';
        break;
      }
      html += '\n';
      for (const [ot, text] of st.outs) {
        if (t >= ot) html += colorize(text);
      }
      if (t < st.outEnd) {
        idle = false;
        break;
      }
      if (!html.endsWith('\n')) html += '\n';
    }
    if (idle) {
      html += PROMPT + '<span class="cur' + (Math.floor(t * 2) % 2 ? ' off' : '') + '"></span>';
    }
    return html;
  }

  function renderTerm(scene, el, t, key) {
    const term = scene.term;
    el.querySelector('.term-heading').textContent = term.heading;
    const body = el.querySelector('.term-body');
    const html = termHtml(term.steps, t);
    if (termCache.get(scene.id) !== html) {
      body.innerHTML = html;
      termCache.set(scene.id, html);
    }
    body.scrollTop = body.scrollHeight;
    const badge = el.querySelector('.term-badge');
    const active = term.steps.find((s) => s.badge && t >= s.badge.t0 && t < s.badge.t1);
    badge.textContent = active ? active.badge.text : '';
    badge.style.opacity = active ? '1' : '0';
    key.push(html.length, html.endsWith('off"></span>') ? 1 : 0, active ? active.id : '-');
  }

  window.renderAt = function (t) {
    const key = [];
    T.scenes.forEach((scene, i) => {
      const el = document.getElementById('scene-' + scene.id);
      const on = t >= scene.start && t < scene.end;
      el.style.display = on ? 'block' : 'none';
      if (!on) return;
      const fadeIn = ease((t - scene.start) / 0.5);
      const fadeOut = i < T.scenes.length - 1 ? 1 - ease((t - (scene.end - 0.35)) / 0.35) : 1;
      el.style.opacity = (fadeIn * fadeOut).toFixed(3);
      key.push(scene.id, el.style.opacity);

      el.querySelectorAll('[data-r]').forEach((node) => {
        const at = scene.reveals[node.dataset.r];
        const p = at === undefined ? 0 : ease((t - at) / 0.6);
        node.style.opacity = p.toFixed(3);
        if (!(node instanceof SVGElement)) node.style.transform = 'translateY(' + ((1 - p) * 18).toFixed(1) + 'px)';
        key.push(p.toFixed(3));
      });
      el.querySelectorAll('[data-hl]').forEach((node) => {
        const n = Number(node.dataset.hl);
        const from = scene.reveals[n];
        const until = scene.reveals[n + 1];
        const lit = from !== undefined && t >= from && (until === undefined || t < until);
        node.classList.toggle('hl', lit);
        key.push(lit ? 1 : 0);
      });
      if (scene.term) renderTerm(scene, el, t, key);
    });

    const cap = T.captions.find((c) => t >= c.t0 && t < c.t1);
    document.querySelector('#caption span').textContent = cap ? cap.text : '';
    const width = ((t / T.duration) * 100).toFixed(2) + '%';
    document.querySelector('#progress i').style.width = width;
    key.push(cap ? cap.t0 : '-', width);
    return key.join('|');
  };

  // Opened directly in a browser (not driven by render.py): play in real time.
  if (!window.__RENDERER__) {
    const t0 = performance.now();
    const tick = () => {
      const t = (performance.now() - t0) / 1000;
      window.renderAt(Math.min(t, T.duration - 0.001));
      if (t < T.duration) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
})();
