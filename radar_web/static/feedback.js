/* Retours utilisateur (bouton 💬, toutes pages) : "pointer" un élément précis
   de la page (ou une note générale) pour éviter l'aller-retour "de quoi tu
   parles ?" — le repère textuel de l'élément touché part avec la note. */
(function () {
  var picking = false, hoverEl = null;

  function shortTarget(el) {
    var txt = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    var tag = el.tagName.toLowerCase();
    var cls = (typeof el.className === 'string' && el.className) ? '.' + el.className.split(' ')[0] : '';
    return txt ? (tag + cls + ' « ' + txt + ' »') : (tag + cls);
  }

  function highlight(el) {
    if (hoverEl) hoverEl.classList.remove('fb-hover');
    hoverEl = el;
    if (hoverEl) hoverEl.classList.add('fb-hover');
  }

  function inWidget(el) {
    return !!(el.closest('#fb-bar') || el.closest('#fb-composer') || el.closest('#fb-toggle'));
  }

  function openComposer(target) {
    stopPicking();
    var box = document.getElementById('fb-composer');
    box.hidden = false;
    box.querySelector('[name=page]').value = location.pathname + location.search;
    box.querySelector('[name=target]').value = target || '';
    box.querySelector('.fb-target-preview').textContent =
      target ? ('à propos de : ' + target) : 'note générale sur cette page';
    box.querySelector('textarea').focus();
  }

  function onMove(ev) {
    var el = document.elementFromPoint(ev.clientX, ev.clientY);
    if (el && !inWidget(el)) highlight(el);
  }
  function onPick(ev) {
    if (inWidget(ev.target)) return;
    ev.preventDefault();
    ev.stopPropagation();
    openComposer(shortTarget(ev.target));
  }

  function startPicking() {
    picking = true;
    document.getElementById('fb-bar').hidden = false;
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onPick, true);
    document.addEventListener('touchend', onPick, true);
  }
  function stopPicking() {
    picking = false;
    document.getElementById('fb-bar').hidden = true;
    highlight(null);
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onPick, true);
    document.removeEventListener('touchend', onPick, true);
  }

  document.body.addEventListener('click', function (ev) {
    if (ev.target.closest('#fb-toggle')) { picking ? stopPicking() : startPicking(); }
    else if (ev.target.id === 'fb-general') { openComposer(''); }
    else if (ev.target.id === 'fb-cancel') { stopPicking(); }
    else if (ev.target.id === 'fb-close') { document.getElementById('fb-composer').hidden = true; }
  });
})();
