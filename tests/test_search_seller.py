"""Champ « Vendeur Discogs » de /search (demande utilisateur 2026-09-18).

Renseigner un nom d'utilisateur Discogs change la SOURCE des sorties — le stock
« For Sale » de ce vendeur — et laisse tous les autres filtres de la page
s'appliquer à l'identique : label, genre, style, période, seuils de score, et le
filtre vinyle implicite de cet écran.

L'inventaire Discogs ne porte que `release_id`, `artist` et `format` : styles,
genres, année et label sont relus dans le référentiel local par identifiant.
D'où les deux cas limites testés ici — sortie absente du référentiel, et sortie
non vinyle.

Depuis le point 68 (demande utilisateur 2026-09-19 : chercher sur TOUT le stock
d'un vendeur, pas sur un échantillon), /search ne lit plus l'API dans la requête
HTTP : il filtre le snapshot complet écrit une fois par le job de fond
`seller_inventory`. Les tests couvrent donc les deux moitiés — la recherche sur
le snapshot, et le job qui le construit.

Aucun accès réseau : `discogs.seller_inventory` est simulée, le référentiel est
une base SQLite synthétique.

Lancer : python3 -m unittest tests.test_search_seller -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="radar-test-")
os.environ.setdefault("CRATE_DATA_DIR", _TMP)

from fastapi.testclient import TestClient  # noqa: E402

import crate_jobs  # noqa: E402
from radar_web import app as appmod  # noqa: E402
from radar_web.radar import (discogs, discogs_dump as dd, jobs, paths,  # noqa: E402
                             sellers as scat, store)

# release_id -> (titre, artiste, label, année, genres, styles, vinyle)
REFERENTIEL = {
    101: ("Deep One", "Artiste A", "Aim Records", 2001, "Electronic", "Deep House", 1),
    102: ("Techno Two", "Artiste B", "Autre Label", 2015, "Electronic", "Techno", 1),
    103: ("Galette CD", "Artiste C", "Aim Records", 2003, "Electronic", "Deep House", 0),
}

LISTINGS = [
    {"release_id": 101, "listing_id": 1, "price": 12.0, "currency": "EUR",
     "condition": "VG+", "sleeve": "VG+", "artist": "Artiste A",
     "format": '12"', "title": "Deep One"},
    {"release_id": 102, "listing_id": 2, "price": 9.0, "currency": "EUR",
     "condition": "NM", "sleeve": "NM", "artist": "Artiste B",
     "format": '12"', "title": "Techno Two"},
    {"release_id": 103, "listing_id": 3, "price": 5.0, "currency": "EUR",
     "condition": "NM", "sleeve": "NM", "artist": "Artiste C",
     "format": "CD", "title": "Galette CD"},
    # absente du référentiel : ajoutée sur Discogs depuis le dernier import mensuel
    {"release_id": 999, "listing_id": 4, "price": 20.0, "currency": "EUR",
     "condition": "M", "sleeve": "M", "artist": "Artiste Neuf",
     "format": '12"', "title": "Sortie Toute Neuve"},
]


def _build_referentiel(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE releases (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "label TEXT, catno TEXT, year INTEGER, country TEXT, format TEXT, "
                "genres TEXT, styles TEXT, master_id INTEGER, is_vinyl INTEGER)")
    for rid, (title, artist, label, year, genres, styles, vinyl) in REFERENTIEL.items():
        con.execute("INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, title, artist, label, "CAT1", year, "FR", '12"',
                     genres, styles, None, vinyl))
    con.commit()
    con.close()


class SearchSellerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = os.path.join(self._tmp.name, "dump.sqlite3")
        _build_referentiel(db)

        patchers = [
            mock.patch.object(appmod, "_dev_mode", return_value=True),
            mock.patch.object(dd, "DB_PATH", db),
            mock.patch.object(dd, "get_meta", return_value={}),
            # snapshots vendeur et file de jobs isolés : ces tests écrivent
            # vraiment sur le disque, jamais dans le /data réel.
            mock.patch.object(scat, "INV_DIR", os.path.join(self._tmp.name, "inv")),
            mock.patch.object(scat, "INV_META_PATH",
                              os.path.join(self._tmp.name, "inv_meta.json")),
            mock.patch.object(jobs, "QUEUE_PATH",
                              os.path.join(self._tmp.name, "queue.json")),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        cfg = store.load_config(paths.DEFAULT_UID)
        cfg["token"] = "tok"
        store.save_config(cfg, paths.DEFAULT_UID)
        self.addCleanup(self._clear_token)
        self.client = TestClient(appmod.app, raise_server_exceptions=False)

    def _clear_token(self):
        cfg = store.load_config(paths.DEFAULT_UID)
        cfg["token"] = ""
        store.save_config(cfg, paths.DEFAULT_UID)

    def _seed(self, listings=None, seller="boutique", partial=False):
        """Écrit le snapshot que le job `seller_inventory` aurait produit."""
        snap = {str(it["release_id"]): {k: v for k, v in it.items() if k != "release_id"}
                for it in (LISTINGS if listings is None else listings)}
        scat.save_inventory(seller, snap)
        scat.set_inv_meta(seller, {"fetched_at": "2026-09-19T10:00:00",
                                   "n_items": len(snap), "n_listings": len(snap),
                                   "n_pages": 1, "partial": partial})
        return snap

    def _post(self, seed=True, **form):
        """POST /search en mode vendeur. `seed` écrit d'abord le snapshot ; aucun
        appel API n'est attendu dans la requête HTTP (le job seul en fait)."""
        if seed:
            self._seed()
        data = {"seller": "boutique", "label": "", "genre": "", "style": "",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"}
        data.update(form)
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel API attendu "
                                                           "dans la requête web")) as inv:
            r = self.client.post("/search", data=data)
        self.assertEqual(r.status_code, 200)
        return r.text, inv

    def test_champ_vendeur_present_sous_le_label(self):
        page = self.client.get("/search").text
        self.assertIn('name="seller"', page)
        self.assertIn("Vendeur Discogs", page)
        self.assertLess(page.index('name="label"'), page.index('name="seller"'))

    def test_stock_du_vendeur_filtre_vinyle(self):
        """La galette CD (103) sort, les deux 12\" restent, et la sortie absente du
        référentiel reste tant qu'aucun filtre ne porte dessus."""
        html, _ = self._post()
        self.assertIn("Deep One", html)
        self.assertIn("Techno Two", html)
        self.assertNotIn("Galette CD", html)
        self.assertIn("Sortie Toute Neuve", html)
        self.assertIn("1 hors vinyle", html)

    def test_filtre_style_applique_au_stock(self):
        html, _ = self._post(style="Deep House")
        self.assertIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_plusieurs_styles_coches_sont_un_OU(self):
        """Contrat explicite (question utilisateur 2026-09-19) : cocher deux
        styles élargit la recherche — une sortie qui n'en porte qu'UN passe.
        Jamais un ET, qui exigerait les deux styles sur le même disque."""
        html, _ = self._post(style="Deep House\nTechno")
        self.assertIn("Deep One", html)          # Deep House seulement
        self.assertIn("Techno Two", html)        # Techno seulement

    def test_date_de_lecture_affichee(self):
        """L'utilisateur doit savoir de quand date le stock qu'il filtre."""
        html, _ = self._post()
        self.assertIn("2026-09-19", html)

    def test_filtre_label_applique_au_stock(self):
        html, _ = self._post(label="Aim Records")
        self.assertIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_filtre_genre_applique_au_stock(self):
        html, _ = self._post(genre="Hip Hop")
        self.assertNotIn("Deep One", html)
        self.assertNotIn("Techno Two", html)

    def test_filtre_periode_applique_au_stock(self):
        html, _ = self._post(year_from="2010", year_to="2020")
        self.assertIn("Techno Two", html)
        self.assertNotIn("Deep One", html)

    def test_sortie_hors_referentiel_ecartee_des_qu_un_filtre_porte_dessus(self):
        """On ne peut pas affirmer qu'une sortie inconnue du référentiel passe un
        filtre de style : elle est écartée, et le nombre est annoncé."""
        html, _ = self._post(style="Deep House")
        self.assertNotIn("Sortie Toute Neuve", html)
        self.assertIn("non filtrable", html)

    def test_lecture_interrompue_signalee(self):
        """Un snapshot partiel (lecture arrêtée) ne doit jamais être présenté
        comme le stock complet du vendeur."""
        self._seed(partial=True)
        html, _ = self._post(seed=False)
        self.assertIn("incomplet", html)

    def test_sans_token_message_explicite(self):
        self._clear_token()
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            r = self.client.post("/search", data={
                "seller": "boutique", "label": "", "genre": "", "style": "",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token Discogs", r.text)

    def test_vendeur_jamais_lu_lance_le_job_sans_appel_api(self):
        """Pas encore de snapshot : la requête web ne va PAS chercher l'inventaire
        elle-même (plusieurs minutes de pagination) — elle enfile le job et le dit."""
        html, _ = self._post(seed=False, seller="nouveau")
        self.assertIn("Lecture du stock", html)
        self.assertIn("nouveau", html)
        q = jobs.load_queue()
        self.assertEqual([(j["name"], j["params"].get("seller")) for j in q],
                         [("seller_inventory", "nouveau")])

    def test_recherche_sur_snapshot_ne_relance_pas_le_job(self):
        html, _ = self._post()
        self.assertIn("Deep One", html)
        self.assertEqual(jobs.load_queue(), [])

    def test_sans_vendeur_le_chemin_habituel_est_inchange(self):
        """Non-régression : un POST sans vendeur ne doit jamais appeler l'inventaire."""
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            r = self.client.post("/search", data={
                "seller": "", "label": "", "genre": "", "style": "Deep House",
                "year_from": "", "year_to": "", "pages": "2", "base_metric": "",
                "base_min": "", "label_min": "", "artist_min": "", "page": "1"})
        self.assertEqual(r.status_code, 200)

    def test_arobase_en_tete_tolere(self):
        """« @boutique » doit lire le snapshot de « boutique », pas en créer un second."""
        html, _ = self._post(seller="@boutique")
        self.assertIn("Deep One", html)
        self.assertEqual(jobs.load_queue(), [])

    def test_bouton_panier_pointe_vers_l_annonce_discogs_du_vendeur(self):
        """En mode vendeur, le bouton n'ajoute plus à la wantlist : il ouvre
        directement la fiche de vente (listing_id, pas release_id) chez Discogs,
        seul endroit où « ajouter au panier » existe réellement (l'API publique
        n'expose aucun endpoint panier — vérifié avant de coder)."""
        html, _ = self._post()
        self.assertIn("https://www.discogs.com/sell/item/1", html)   # Deep One
        self.assertIn("https://www.discogs.com/sell/item/2", html)   # Techno Two
        self.assertNotIn("Ajouter à la wantlist", html)
        self.assertNotIn("hx-post=\"/cart/add\"", html)

    def test_hors_mode_vendeur_le_bouton_wantlist_est_inchange(self):
        """Non-régression au niveau du gabarit (pas de la base locale, dont le
        schéma minimal de ce fixture n'exerce pas `search_local`) : sans vendeur,
        le bouton wantlist reste affiché, jamais un lien vers une annonce."""
        raw = [{"id": 101, "title": "Artiste A - Deep One", "label": ["Aim Records"],
                "style": ["Deep House"], "catno": "CAT1", "year": 2001,
                "cover_image": None, "thumb": None, "uri": "/release/101"}]
        results = appmod._scored_rows(appmod.Ctx(paths.DEFAULT_UID), raw)
        html = appmod.templates.env.get_template("partials/results.html").render(
            results=results, seller="", seller_note=None, searched=[], dump_date=None,
            voted={}, in_cart={}, empty_reason=None, has_token=True,
            n_matches=len(results), page=1, total_pages=1)
        self.assertIn("Ajouter à la wantlist", html)
        self.assertIn('hx-post="/cart/add"', html)
        self.assertNotIn("discogs.com/sell/item", html)


class FakeJob:
    """Duck-type de `crate_jobs.Job` : suit done/total comme le vrai (le job lit
    `st["done"]` pour journaliser le nombre de pages) et retient l'erreur, que
    `finish(error=...)` doit remonter telle quelle."""

    def __init__(self, stopped_after=None):
        self.st = {"done": 0, "total": 0}
        self.messages, self.ticks = [], []
        self.finished = self.error = None
        self._stopped_after = stopped_after

    def stopped(self):
        return (self._stopped_after is not None
                and self.st["done"] >= self._stopped_after)

    def tick(self, last="", inc=1, total=None):
        self.st["done"] += inc
        if total is not None:
            self.st["total"] = total
        self.ticks.append(last)

    def msg(self, m):
        self.messages.append(m)

    def sub(self, done=None, total=None, label=None):
        pass

    def finish(self, message="", error=None):
        self.finished, self.error = message, error


class JobSellerInventoryTestCase(unittest.TestCase):
    """Le job de fond qui lit TOUT le stock d'un vendeur (point 68). Discogs
    pagine 100 articles à la fois, cadencés à 1,1 s : c'est ce qui interdit de le
    faire dans la requête web, d'où ce job. `discogs.seller_inventory` est
    simulée — aucun réseau."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patchers = [
            mock.patch.object(scat, "INV_DIR", os.path.join(self._tmp.name, "inv")),
            mock.patch.object(scat, "INV_META_PATH",
                              os.path.join(self._tmp.name, "inv_meta.json")),
            mock.patch.object(crate_jobs, "cfg_load", return_value={"token": "tok"}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_ecrit_le_snapshot_complet_et_sa_fraicheur(self):
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                return_value=(LISTINGS, False)) as inv:
            crate_jobs.job_seller_inventory(job, {"seller": "boutique"})
        self.assertEqual(inv.call_args.kwargs["max_pages"], 0)     # pas de plafond
        snap = scat.load_inventory("boutique")
        self.assertEqual(sorted(snap), ["101", "102", "103", "999"])
        self.assertEqual(snap["101"]["listing_id"], 1)
        self.assertEqual(snap["101"]["title"], "Deep One")         # sert hors référentiel
        meta = scat.inv_meta("boutique")
        self.assertEqual(meta["n_items"], 4)
        self.assertFalse(meta["partial"])
        self.assertIn("4 disque(s)", job.finished)

    def test_arobase_et_espaces_normalises(self):
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                return_value=(LISTINGS, False)) as inv:
            crate_jobs.job_seller_inventory(job, {"seller": " @boutique "})
        self.assertEqual(inv.call_args[0][0], "boutique")
        self.assertTrue(scat.load_inventory("boutique"))

    def test_avancement_page_par_page_et_arret_demande(self):
        """L'avancement passe par `on_page` ; renvoyer False (arrêt demandé)
        doit couper la pagination au lieu de lire tout le stock."""
        pages = 5

        def _fake(username, token="", max_pages=0, per_page=100, on_page=None):
            got = []
            for page in range(1, pages + 1):
                got.extend(LISTINGS)
                if on_page and on_page(len(got), page, pages) is False:
                    return got, page < pages
            return got, False

        job = FakeJob(stopped_after=2)
        with mock.patch.object(discogs, "seller_inventory", side_effect=_fake):
            crate_jobs.job_seller_inventory(job, {"seller": "boutique"})
        self.assertEqual(job.st["done"], 2)                        # coupé à la 2e page
        self.assertEqual(job.st["total"], pages)
        self.assertIn("interrompue", job.finished)

    def test_lecture_interrompue_ne_remplace_pas_un_snapshot_complet(self):
        """Un stock partiel ne doit jamais écraser un inventaire complet déjà lu."""
        scat.save_inventory("boutique", {"101": {"listing_id": 1}, "102": {"listing_id": 2}})
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                return_value=(LISTINGS[:1], True)):
            crate_jobs.job_seller_inventory(job, {"seller": "boutique"})
        self.assertEqual(sorted(scat.load_inventory("boutique")), ["101", "102"])
        self.assertIn("conservé", job.finished)

    def test_premier_passage_interrompu_garde_ce_qui_a_ete_lu(self):
        """Sans snapshot antérieur, mieux vaut un stock partiel annoncé comme tel
        que rien du tout."""
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                return_value=(LISTINGS[:2], True)):
            crate_jobs.job_seller_inventory(job, {"seller": "boutique"})
        self.assertEqual(sorted(scat.load_inventory("boutique")), ["101", "102"])
        self.assertTrue(scat.inv_meta("boutique")["partial"])

    def test_vendeur_inconnu_remonte_l_erreur_discogs(self):
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=discogs.DiscogsError("Erreur Discogs 404 : not found")):
            crate_jobs.job_seller_inventory(job, {"seller": "nexistepas"})
        self.assertIn("nexistepas", job.error)
        self.assertIn("404", job.error)
        self.assertEqual(scat.load_inventory("nexistepas"), {})

    def test_sans_vendeur_message_explicite(self):
        job = FakeJob()
        with mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            crate_jobs.job_seller_inventory(job, {})
        self.assertIn("Aucun vendeur", job.error)

    def test_sans_token_pas_d_appel(self):
        job = FakeJob()
        with mock.patch.object(crate_jobs, "cfg_load", return_value={}), \
             mock.patch.object(discogs, "seller_inventory",
                                side_effect=AssertionError("aucun appel attendu")):
            crate_jobs.job_seller_inventory(job, {"seller": "boutique"})
        self.assertIn("token Discogs", job.error)


class SellerInventoryPaginationTestCase(unittest.TestCase):
    """`discogs.seller_inventory` : plafond de pages retiré (`max_pages=0`) pour
    la lecture exhaustive, et rappel d'avancement."""

    def _resp(self, pages):
        def _get(path, params=None, token=""):
            page = (params or {}).get("page", 1)
            return {"listings": [{"id": 100 + page,
                                  "price": {"value": 5, "currency": "EUR"},
                                  "release": {"id": page, "artist": "A", "title": "T",
                                              "format": '12"'}}],
                    "pagination": {"pages": pages, "items": pages}}
        return _get

    def test_sans_plafond_toutes_les_pages(self):
        seen = []
        with mock.patch.object(discogs, "get", side_effect=self._resp(4)), \
             mock.patch.object(discogs.time, "sleep"):
            out, truncated = discogs.seller_inventory(
                "x", token="t", max_pages=0,
                on_page=lambda n, page, pages: seen.append((n, page, pages)))
        self.assertEqual(len(out), 4)
        self.assertFalse(truncated)
        self.assertEqual(seen, [(1, 1, 4), (2, 2, 4), (3, 3, 4), (4, 4, 4)])

    def test_plafond_explicite_tronque(self):
        with mock.patch.object(discogs, "get", side_effect=self._resp(10)), \
             mock.patch.object(discogs.time, "sleep"):
            out, truncated = discogs.seller_inventory("x", token="t", max_pages=2)
        self.assertEqual(len(out), 2)
        self.assertTrue(truncated)

    def test_on_page_qui_renvoie_false_interrompt(self):
        with mock.patch.object(discogs, "get", side_effect=self._resp(10)), \
             mock.patch.object(discogs.time, "sleep"):
            out, truncated = discogs.seller_inventory(
                "x", token="t", max_pages=0, on_page=lambda n, page, pages: page < 3)
        self.assertEqual(len(out), 3)
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
