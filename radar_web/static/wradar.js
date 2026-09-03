/* Toile d'araignée des pondérations (Réglages).
   Composant complémentaire : les <input type="number"> restent la source de
   vérité (soumission du formulaire, clavier, accessibilité) ; la toile ne fait
   que lire et écrire leur valeur. Sans JS, le conteneur reste [hidden]. */
(function () {
  var NS = 'http://www.w3.org/2000/svg';
  var W = 300, H = 250, CX = 150, CY = 118, R = 76, GRAB = 34;

  function svgEl(name, attrs) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    return e;
  }

  function fmt(v) { return String(Math.round(v * 1000) / 1000); }

  function build(box) {
    var spec;
    try { spec = JSON.parse(box.dataset.radar || '[]'); } catch (e) { return; }
    var form = box.closest('form');
    if (!form) return;
    var step = parseFloat(box.dataset.step) || 0.05;

    var axes = [];
    spec.forEach(function (s) {
      var input = form.querySelector('[name="' + s[0] + '"]');
      if (input) axes.push({ label: s[1] || s[0], input: input });
    });
    if (axes.length < 3) return;   // une toile a besoin d'au moins trois axes

    var n = axes.length, syncing = false, dragging = -1, max = 1;

    var svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H, 'aria-hidden': 'true', focusable: 'false'
    });
    var gGrid = svgEl('g'), gAxes = svgEl('g'), gLabels = svgEl('g');
    var poly = svgEl('polygon', { style: 'fill:var(--pin);fill-opacity:.13;stroke:var(--pin);stroke-width:2;stroke-linejoin:round' });
    var gHandles = svgEl('g');
    svg.appendChild(gGrid); svg.appendChild(gAxes); svg.appendChild(poly);
    svg.appendChild(gHandles); svg.appendChild(gLabels);

    function angle(i) { return -Math.PI / 2 + i * 2 * Math.PI / n; }
    function pt(i, r) {
      var a = angle(i);
      return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
    }

    [0.25, 0.5, 0.75, 1].forEach(function (lvl) {
      var pts = [];
      for (var i = 0; i < n; i++) pts.push(pt(i, R * lvl).join(','));
      gGrid.appendChild(svgEl('polygon', {
        points: pts.join(' '),
        style: 'fill:none;stroke:var(--line);stroke-width:1'
      }));
    });
    for (var i = 0; i < n; i++) {
      var p = pt(i, R);
      gAxes.appendChild(svgEl('line', {
        x1: CX, y1: CY, x2: p[0], y2: p[1], style: 'stroke:var(--line);stroke-width:1'
      }));
    }

    axes.forEach(function (ax, i) {
      var p = pt(i, R + 14), anchor = 'middle';
      if (p[0] > CX + 6) anchor = 'start';
      else if (p[0] < CX - 6) anchor = 'end';
      ax.tName = svgEl('text', {
        x: p[0], y: p[1], 'text-anchor': anchor,
        style: 'fill:var(--soft);font-size:10.5px;font-family:inherit'
      });
      ax.tName.textContent = ax.label;
      // la valeur se pose vers l'extérieur, pour ne pas empiéter sur la toile
      ax.tVal = svgEl('text', {
        x: p[0], y: p[1] + (p[1] < CY - 6 ? -12 : 12), 'text-anchor': anchor,
        style: 'fill:var(--ink);font-size:11px;font-weight:600;font-family:inherit'
      });
      gLabels.appendChild(ax.tName); gLabels.appendChild(ax.tVal);

      ax.hit = svgEl('circle', { r: 18, style: 'fill:transparent' });
      ax.dot = svgEl('circle', {
        r: 6, style: 'fill:var(--surface);stroke:var(--pin);stroke-width:2.5'
      });
      gHandles.appendChild(ax.hit); gHandles.appendChild(ax.dot);
    });

    function readValue(ax) {
      var v = parseFloat(ax.input.value);
      return isFinite(v) ? v : 0;
    }

    function draw() {
      var vals = axes.map(readValue), top = 1;
      vals.forEach(function (v) { if (v > top) top = v; });
      // domaine arrondi au demi supérieur : la toile reste lisible même si une
      // valeur saisie au clavier dépasse 1
      max = Math.ceil(top * 2 - 1e-9) / 2;
      var pts = [];
      axes.forEach(function (ax, i) {
        var v = Math.max(0, Math.min(max, vals[i]));
        var p = pt(i, R * (max ? v / max : 0));
        pts.push(p.join(','));
        ax.dot.setAttribute('cx', p[0]); ax.dot.setAttribute('cy', p[1]);
        ax.hit.setAttribute('cx', p[0]); ax.hit.setAttribute('cy', p[1]);
        ax.tVal.textContent = fmt(vals[i]);
      });
      poly.setAttribute('points', pts.join(' '));
      scale.textContent = 'échelle 0 → ' + fmt(max);
    }

    function setValue(i, v) {
      v = Math.max(0, Math.min(max, Math.round(v / step) * step));
      var ax = axes[i];
      if (fmt(v) === fmt(readValue(ax))) return;
      syncing = true;
      ax.input.value = fmt(v);
      ax.input.dispatchEvent(new Event('input', { bubbles: true }));
      ax.input.dispatchEvent(new Event('change', { bubbles: true }));
      syncing = false;
      draw();
    }

    function local(ev) {
      var r = svg.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return { x: (ev.clientX - r.left) / r.width * W,
               y: (ev.clientY - r.top) / r.height * H };
    }

    function pickAxis(q) {
      var best = -1, bd = Infinity, i, d, dx, dy;
      for (i = 0; i < n; i++) {
        dx = q.x - +axes[i].dot.getAttribute('cx');
        dy = q.y - +axes[i].dot.getAttribute('cy');
        d = Math.sqrt(dx * dx + dy * dy);
        if (d < bd) { bd = d; best = i; }
      }
      if (bd <= GRAB) return best;
      // hors de la toile (zone des étiquettes) : on ne touche à rien
      if (Math.sqrt((q.x - CX) * (q.x - CX) + (q.y - CY) * (q.y - CY)) > R + 6) return -1;
      // loin de toute poignée : on prend l'axe dont la direction est la plus
      // proche du doigt (permet de « poser » une valeur d'un seul appui)
      var a = Math.atan2(q.y - CY, q.x - CX), ba = Infinity;
      best = -1;
      for (i = 0; i < n; i++) {
        d = Math.abs(Math.atan2(Math.sin(a - angle(i)), Math.cos(a - angle(i))));
        if (d < ba) { ba = d; best = i; }
      }
      return best;
    }

    function apply(i, q) {
      var a = angle(i);
      // projection sur l'axe : seule la composante radiale compte, le doigt
      // peut donc dériver latéralement sans faire sauter la valeur
      var r = (q.x - CX) * Math.cos(a) + (q.y - CY) * Math.sin(a);
      setValue(i, max * Math.max(0, Math.min(R, r)) / R);
    }

    svg.addEventListener('pointerdown', function (ev) {
      var q = local(ev);
      if (!q) return;
      dragging = pickAxis(q);
      if (dragging < 0) return;
      ev.preventDefault();
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* pointeur déjà parti */ }
      axes[dragging].dot.setAttribute('r', 8);
      apply(dragging, q);
    });
    svg.addEventListener('pointermove', function (ev) {
      if (dragging < 0) return;
      var q = local(ev);
      if (!q) return;
      ev.preventDefault();
      apply(dragging, q);
    });
    function stop(ev) {
      if (dragging < 0) return;
      axes[dragging].dot.setAttribute('r', 6);
      dragging = -1;
      if (svg.releasePointerCapture && ev.pointerId != null) {
        try { svg.releasePointerCapture(ev.pointerId); } catch (e) { /* déjà relâché */ }
      }
    }
    svg.addEventListener('pointerup', stop);
    svg.addEventListener('pointercancel', stop);

    axes.forEach(function (ax) {
      ax.input.addEventListener('input', function () { if (!syncing) draw(); });
      ax.input.addEventListener('change', function () { if (!syncing) draw(); });
    });
    form.addEventListener('reset', function () { setTimeout(draw, 0); });

    box.appendChild(svg);
    var scale = document.createElement('p');
    scale.className = 'small muted wradar__scale';
    box.appendChild(scale);
    var hint = document.createElement('p');
    hint.className = 'small muted wradar__hint';
    hint.textContent = 'Glisse les points (doigt ou souris) — les champs chiffrés suivent.';
    box.appendChild(hint);

    draw();
    box.hidden = false;
    // la toile affiche déjà chaque valeur à côté de son axe : les champs
    // chiffrés doublonnent l'info une fois le JS chargé — masqués (pas
    // supprimés : ils restent la source de vérité du formulaire), pour
    // qu'un navigateur sans JS les retrouve intacts.
    axes.forEach(function (ax) {
      var wrap = ax.input.closest('.field');
      if (wrap) wrap.hidden = true;
    });
  }

  function init(root) {
    (root || document).querySelectorAll('[data-radar]').forEach(function (box) {
      if (box.dataset.ready) return;
      box.dataset.ready = '1';
      build(box);
    });
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', function () { init(); });
  else init();
  document.body.addEventListener('htmx:afterSwap', function (e) { init(e.target); });
})();
