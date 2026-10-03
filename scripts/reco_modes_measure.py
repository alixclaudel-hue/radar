#!/usr/bin/env python3
'''Mesure les volumes de pistes candidates pour le mode Découverte de Reco Radar.'''

import argparse
import json
import sys
from os.path import abspath, dirname
from typing import Any

# L’ajout du dépôt permet d’exécuter directement ce fichier depuis une worktree.
sys.path.insert(0, dirname(dirname(abspath(__file__))))

from radar_web.radar import catalog_labelgraph, paths, scorestore
from radar_web.radar.scoring import Ctx


TAILLE_LOT = 400


class StatistiquesVoie:
    def __init__(self) -> None:
        self.total = 0
        self.score60 = 0
        self.artists = set()
        self.labels = set()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Mesure les modes de reco Découverte'
    )
    parser.add_argument(
        '--uid',
        default=paths.DEFAULT_UID,
        help='UID du compte utilisateur',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Afficher le rapport au format JSON',
    )
    parser.add_argument(
        '--top',
        type=int,
        default=30,
        help='Nombre maximal de pistes à afficher',
    )
    args = parser.parse_args()

    if args.top < 0:
        parser.error('--top doit être supérieur ou égal à 0')

    uid = args.uid
    if not scorestore.available(uid):
        sys.exit(f'Aucun store de scores n’est disponible pour l’UID {uid}.')
    if not catalog_labelgraph.available():
        sys.exit('Le catalogue de labels n’est pas disponible.')

    ctx = Ctx(uid=args.uid)

    connus = set(ctx.artist_tier_map())
    for enregistrement in ctx.corpus:
        artiste_corpus = enregistrement.get('artist')
        if artiste_corpus:
            connus.add(ctx.canon_artist_key(artiste_corpus))
    for artiste_collection in ctx.collection.get('artist_counts', {}):
        connus.add(ctx.canon_artist_key(artiste_collection))

    # Le graphe fournit des clés déjà canoniques : les normaliser à nouveau pourrait les altérer.
    a1: dict[str, Any] = {
        artiste: donnees
        for artiste, donnees in ctx.graph_rescore().get('artists', {}).items()
        if artiste not in connus
    }

    l_tier = ctx.label_tier_map()
    l1: dict[str, dict[str, Any]] = {}
    seeds_l1 = list(l_tier)

    # Les lots limitent la taille des traitements sans changer les ensembles obtenus.
    for debut in range(0, len(seeds_l1), TAILLE_LOT):
        voisins_l1 = catalog_labelgraph.neighbors_for(
            seeds_l1[debut:debut + TAILLE_LOT]
        )
        for _seed_l1, elements in voisins_l1.items():
            for element in elements:
                label_key = element.get('label_key')
                if not label_key or label_key in l_tier:
                    continue
                poids = element.get('weight', 1)
                element_precedent = l1.get(label_key)
                if (
                    element_precedent is None
                    or poids > element_precedent.get('weight', 0)
                ):
                    l1[label_key] = element

    l1_keys = list(l1)
    via: dict[str, set[str]] = {}
    maxw: dict[str, Any] = {}
    l2_avant_filtre: dict[str, dict[str, Any]] = {}

    # Les preuves sont fusionnées sur tous les lots, car un label peut sembler faible dans chaque lot pris isolément.
    for debut in range(0, len(l1_keys), TAILLE_LOT):
        lot = l1_keys[debut:debut + TAILLE_LOT]
        voisins_l2 = catalog_labelgraph.neighbors_for(lot)
        for source_l1, elements in voisins_l2.items():
            for element in elements:
                label_key = element.get('label_key')
                if (
                    not label_key
                    or label_key in l_tier
                    or label_key in l1
                ):
                    continue

                poids = element.get('weight', 1)
                via.setdefault(label_key, set()).add(source_l1)
                maxw[label_key] = max(maxw.get(label_key, 0), poids)

                element_precedent = l2_avant_filtre.get(label_key)
                if (
                    element_precedent is None
                    or poids > element_precedent.get('weight', 0)
                ):
                    l2_avant_filtre[label_key] = element

    l2: dict[str, dict[str, Any]] = {
        label_key: element
        for label_key, element in l2_avant_filtre.items()
        if len(via[label_key]) >= 2 or maxw[label_key] >= 2
    }

    noms_voies = (
        'artiste',
        'label_1',
        'label_2',
        'deux_voies',
        'approfondir',
    )
    stats_voies = {nom: StatistiquesVoie() for nom in noms_voies}
    pistes_decouverte: list[dict[str, Any]] = []
    labels_avec_pistes: set[str] = set()

    con = scorestore.connect_readonly(uid)
    try:
        cursor = con.cursor()
        cursor.execute(
            "SELECT ts.artist, ts.title, ts.score, rs.label, rs.label_key FROM track_scores ts JOIN release_scores rs ON rs.release_id = ts.release_id WHERE TRIM(ts.artist) <> ''"
        )
        for artist, title, score, label, label_key in cursor.fetchall():
            if label_key:
                labels_avec_pistes.add(label_key)

            if score is None or score < 0:
                continue

            artiste_canon = ctx.canon_artist_key(artist or '')
            cle_label = label_key or ''
            est_artiste = artiste_canon in a1
            est_label_1 = cle_label in l1
            est_label_2 = cle_label in l2

            if est_artiste and (est_label_1 or est_label_2):
                voie = 'deux_voies'
            elif est_artiste:
                voie = 'artiste'
            elif est_label_1:
                voie = 'label_1'
            elif est_label_2:
                voie = 'label_2'
            else:
                voie = 'approfondir'

            stats_voies[voie].total += 1
            if score >= 60:
                stats_voies[voie].score60 += 1
            stats_voies[voie].artists.add(artiste_canon)
            if cle_label:
                stats_voies[voie].labels.add(cle_label)

            if voie != 'approfondir':
                pistes_decouverte.append(
                    {
                        'artist': artist or '',
                        'title': title or '',
                        'label': label or '',
                        'voie': voie,
                        'score': score,
                    }
                )
    finally:
        con.close()

    pistes_decouverte.sort(
        key=lambda piste: piste['score'],
        reverse=True,
    )
    top_decouverte = pistes_decouverte[:args.top]

    l1_sans_pistes = set(l1) - labels_avec_pistes
    l2_sans_pistes = set(l2) - labels_avec_pistes
    l1_zero_part = (
        len(l1_sans_pistes) / len(l1) * 100
        if l1
        else 0.0
    )
    l2_zero_part = (
        len(l2_sans_pistes) / len(l2) * 100
        if l2
        else 0.0
    )

    resultat: dict[str, Any] = {
        'tailles': {
            'A1': len(a1),
            'L1': len(l1),
            'L2_avant_filtre': len(l2_avant_filtre),
            'L2': len(l2),
        },
        'voies': {
            nom: {
                'total': stats.total,
                'score60': stats.score60,
                'artists_distincts': len(stats.artists),
                'labels_distincts': len(stats.labels),
            }
            for nom, stats in stats_voies.items()
        },
        'labels_sans_pistes': {
            'L1_part_pourcent': round(l1_zero_part, 2),
            'L2_part_pourcent': round(l2_zero_part, 2),
        },
        'top_decouverte': top_decouverte,
        'note_deuxieme_saut_artistes': (
            'Le 2e saut côté artistes n’est pas mesuré.'
        ),
    }

    if args.json:
        print(json.dumps(resultat, ensure_ascii=False, indent=2))
    else:
        print(f'=== RAPPORT MESURE MODES DECOUVERTE (UID: {uid}) ===')
        print(
            f'Tailles des ensembles -> A1: {len(a1)} | L1: {len(l1)} | '
            f'L2 avant filtre: {len(l2_avant_filtre)} | '
            f'L2 après filtre: {len(l2)}'
        )
        print('\n--- Pistes par voie ---')
        for nom, stats in stats_voies.items():
            print(
                f'  - {nom:11s} : total={stats.total:4d} | '
                f'score>=60={stats.score60:4d} | '
                f'artistes={len(stats.artists):4d} | '
                f'labels={len(stats.labels):4d}'
            )

        print('\n--- Labels sans piste dans track_scores ---')
        print(
            f'  - L1 sans piste : {len(l1_sans_pistes)}/{len(l1)} '
            f'({l1_zero_part:.1f}%)'
        )
        print(
            f'  - L2 sans piste : {len(l2_sans_pistes)}/{len(l2)} '
            f'({l2_zero_part:.1f}%)'
        )

        print(f'\n--- Top {args.top} pistes Découverte ---')
        if not top_decouverte:
            print('  (aucune)')
        for rang, piste in enumerate(top_decouverte, 1):
            artiste = piste['artist']
            titre = piste['title']
            label = piste['label']
            voie = piste['voie']
            score = piste['score']
            print(
                f'{rang:2d}. {artiste} — {titre} ({label}) '
                f'[{voie}] : {score}'
            )

        print(
            '\n[Note] Le 2e saut côté artistes n’est pas mesuré.'
        )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
