/* Rangées façon wantlist (Mon univers -> panier, Nouveautés -> veille/vendeurs) :
   toucher une ligne .wl-row charge ses tracks la première fois, les replie/déplie
   ensuite sans nouvel appel réseau. Délégation sur document.body : fonctionne même
   après un swap htmx qui remplace la liste en entier (#cart-list, #inbox-*). */
(function () {
  document.body.addEventListener('click', function (ev) {
    var row = ev.target.closest('.wl-row');
    if (!row) return;
    var tl = row.nextElementSibling;
    if (!tl || !tl.classList.contains('tracklist')) return;
    if (row.dataset.loaded === '1') {
      tl.hidden = !tl.hidden;
      row.classList.toggle('open', !tl.hidden);
      return;
    }
    row.dataset.loaded = '1';
    row.classList.add('open');
    tl.hidden = false;
    htmx.ajax('GET', row.dataset.tracksUrl, { target: tl, swap: 'innerHTML' });
  });
})();
