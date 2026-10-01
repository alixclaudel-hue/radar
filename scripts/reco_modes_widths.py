#!/usr/bin/env python3
'''Mesure la largeur des voisinages pour un futur mode Découverte.'''

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from os.path import abspath, dirname
from typing import Any, Sequence

# La racine du dépôt est ajoutée pour permettre une exécution directe depuis scripts/.
sys.path.insert(0, dirname(dirname(abspath(__file__))))

from radar_web.radar import catalog_labelgraph, discogs_dump, paths
from radar_web.radar.scoring import Ctx


BATCH_SIZE = 400
SCORESTORE_PER_LABEL_LIMIT = 500


@dataclass(frozen=True, slots=True)
class LabelNeighbor:
    label_key: str
    weight: int
    kind: str
    role: str


@dataclass(frozen=True, slots=True)
class LabelSelection:
    label_key: str
    seed_key: str
    weight: int
    seed_tier: str


@dataclass(frozen=True, slots=True)
class ArtistNeighbor:
    artist_key: str
    n: int


def parse_nonnegative_ints(value: str) -> tuple[int, ...]:
    parts = [part.strip() for part in value.split(',')]
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError(
            'Le réglage doit contenir des entiers non négatifs séparés par des virgules.'
        )

    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            'Le réglage doit contenir uniquement des entiers non négatifs.'
        ) from exc

    if any(number < 0 for number in numbers):
        raise argparse.ArgumentTypeError(
            'Le réglage doit contenir uniquement des entiers non négatifs.'
        )

    return tuple(dict.fromkeys(numbers))


def fetch_label_neighbors(
    label_seeds: dict[str, str],
    min_weight: int,
) -> dict[str, list[dict[str, Any]]]:
    seed_keys = sorted(label_seeds)
    neighbors_by_seed: dict[str, list[dict[str, Any]]] = {
        seed: [] for seed in seed_keys
    }

    # Chaque lot effectue un seul appel au seuil global, jamais un appel par valeur de w.
    for start in range(0, len(seed_keys), BATCH_SIZE):
        batch = seed_keys[start : start + BATCH_SIZE]
        batch_result = catalog_labelgraph.neighbors_for(
            batch,
            min_weight=min_weight,
        )
        for seed_key, neighbors in batch_result.items():
            neighbors_by_seed.setdefault(seed_key, []).extend(neighbors)

    return neighbors_by_seed


def prepare_label_neighbors(
    raw_neighbors: dict[str, list[dict[str, Any]]],
    label_seeds: dict[str, str],
) -> dict[str, list[LabelNeighbor]]:
    prepared: dict[str, list[LabelNeighbor]] = {}

    for seed_key in sorted(label_seeds):
        neighbors = [
            LabelNeighbor(
                label_key=str(record['label_key']),
                weight=int(record['weight']),
                kind=str(record['kind']),
                role=str(record['role']),
            )
            for record in raw_neighbors.get(seed_key, [])
        ]
        # Le tri secondaire rend les égalités de poids reproductibles.
        neighbors.sort(
            key=lambda neighbor: (
                -neighbor.weight,
                neighbor.label_key,
                neighbor.kind,
                neighbor.role,
            )
        )
        prepared[seed_key] = neighbors

    return prepared


def select_label_neighborhood(
    k: int,
    w: int,
    label_seeds: dict[str, str],
    prepared_neighbors: dict[str, list[LabelNeighbor]],
) -> list[LabelSelection]:
    if k == 0:
        return []

    selected: list[LabelSelection] = []

    for seed_key in sorted(label_seeds):
        already_selected: set[str] = set()

        for neighbor in prepared_neighbors.get(seed_key, []):
            if neighbor.weight < w:
                continue
            if neighbor.label_key in label_seeds:
                continue
            if neighbor.label_key in already_selected:
                continue

            already_selected.add(neighbor.label_key)
            selected.append(
                LabelSelection(
                    label_key=neighbor.label_key,
                    seed_key=seed_key,
                    weight=neighbor.weight,
                    seed_tier=label_seeds[seed_key],
                )
            )
            if len(already_selected) == k:
                break

    return selected


def invert_artist_edges(
    graph_edges: dict[str, Any],
    artist_seeds: dict[str, str],
) -> dict[str, list[ArtistNeighbor]]:
    inverted: dict[str, list[ArtistNeighbor]] = {
        seed_key: [] for seed_key in artist_seeds
    }

    for artist_key, payload in graph_edges.items():
        for seed_key, credit in payload.get('co', {}).items():
            if seed_key not in artist_seeds:
                continue
            inverted[seed_key].append(
                ArtistNeighbor(
                    artist_key=str(artist_key),
                    n=int(credit['n']),
                )
            )

    for neighbors in inverted.values():
        neighbors.sort(key=lambda neighbor: (-neighbor.n, neighbor.artist_key))

    return inverted


def select_artist_union(
    k: int,
    w: int,
    artist_seeds: dict[str, str],
    inverted_edges: dict[str, list[ArtistNeighbor]],
) -> frozenset[str]:
    if k == 0:
        return frozenset()

    union: set[str] = set()

    for seed_key in sorted(artist_seeds):
        kept = 0
        already_selected: set[str] = set()

        for neighbor in inverted_edges.get(seed_key, []):
            if neighbor.n < w:
                continue
            if neighbor.artist_key in artist_seeds:
                continue
            if neighbor.artist_key in already_selected:
                continue

            already_selected.add(neighbor.artist_key)
            union.add(neighbor.artist_key)
            kept += 1
            if kept == k:
                break

    return frozenset(union)


def load_release_counts(label_keys: set[str]) -> dict[str, int]:
    if not label_keys:
        return {}

    sorted_keys = sorted(label_keys)
    counts: dict[str, int] = {}

    # Le mode ro et immutable garantit une lecture sans écriture ni coordination de verrous.
    connection = sqlite3.connect(
        f'file:{discogs_dump.DB_PATH}?mode=ro&immutable=1',
        uri=True,
    )
    with closing(connection):
        for start in range(0, len(sorted_keys), BATCH_SIZE):
            batch = sorted_keys[start : start + BATCH_SIZE]
            placeholders = ','.join('?' for _ in batch)
            query = (
                'SELECT label_key, COUNT(*) FROM releases '
                f'WHERE label_key IN ({placeholders}) '
                'GROUP BY label_key'
            )
            rows = connection.execute(query, tuple(batch)).fetchall()
            for label_key, count in rows:
                counts[str(label_key)] = int(count)

    return counts


def nearest_rank_quantile(
    sorted_values: Sequence[int],
    percentile: int,
) -> int | None:
    if not sorted_values:
        return None
    rank = (percentile * len(sorted_values) + 99) // 100 - 1
    return sorted_values[rank]


def percentage(count: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(count * 100.0 / total, 2)


def build_report(
    uid: str,
    ctx: Ctx,
    ks: tuple[int, ...],
    ws: tuple[int, ...],
) -> dict[str, Any]:
    label_tier_map: dict[str, str] = ctx.label_tier_map()
    artist_tier_map: dict[str, str] = ctx.artist_tier_map()
    graph: dict[str, Any] = ctx.graph
    graph_edges: dict[str, Any] = graph.get('edges', {})

    minimum_weight = min(ws)
    raw_label_neighbors = fetch_label_neighbors(
        label_tier_map,
        min_weight=minimum_weight,
    )
    edge_weights = sorted(
        int(record['weight'])
        for neighbors in raw_label_neighbors.values()
        for record in neighbors
    )
    prepared_label_neighbors = prepare_label_neighbors(
        raw_label_neighbors,
        label_tier_map,
    )
    inverted_artist_edges = invert_artist_edges(
        graph_edges,
        artist_tier_map,
    )

    provenance_by_setting: dict[
        tuple[int, int], tuple[frozenset[str], frozenset[str]]
    ] = {}
    artist_union_by_setting: dict[tuple[int, int], frozenset[str]] = {}
    all_l1_labels: set[str] = set()

    for k in ks:
        for w in ws:
            label_selections = select_label_neighborhood(
                k,
                w,
                label_tier_map,
                prepared_label_neighbors,
            )
            l1_labels = frozenset(
                selection.label_key for selection in label_selections
            )

            # Chaque catégorie compte ses labels une fois, même si plusieurs graines les atteignent.
            from_category_1 = frozenset(
                selection.label_key
                for selection in label_selections
                if selection.seed_tier == '1'
            )
            from_category_2 = frozenset(
                selection.label_key
                for selection in label_selections
                if selection.seed_tier == '2'
            )
            artist_union = select_artist_union(
                k,
                w,
                artist_tier_map,
                inverted_artist_edges,
            )

            provenance_by_setting[(k, w)] = (
                from_category_1,
                from_category_2,
            )
            artist_union_by_setting[(k, w)] = artist_union
            all_l1_labels.update(l1_labels)

    # Une seule lecture SQLite dessert tous les réglages afin de ne pas répéter les COUNT(*).
    release_counts = load_release_counts(all_l1_labels)

    combinations: list[dict[str, Any]] = []
    for k in ks:
        for w in ws:
            from_category_1, from_category_2 = provenance_by_setting[(k, w)]
            l1_labels = from_category_1 | from_category_2
            widening_cost = sum(
                min(
                    release_counts.get(label_key, 0),
                    SCORESTORE_PER_LABEL_LIMIT,
                )
                for label_key in l1_labels
            )
            labels_without_releases = sum(
                release_counts.get(label_key, 0) == 0
                for label_key in l1_labels
            )

            combinations.append(
                {
                    'k': k,
                    'w': w,
                    'l1': {
                        'taille': len(l1_labels),
                        'provenance_graines': {
                            'cat_1': {
                                'labels': len(from_category_1),
                                'pourcent': percentage(
                                    len(from_category_1),
                                    len(l1_labels),
                                ),
                            },
                            'cat_2': {
                                'labels': len(from_category_2),
                                'pourcent': percentage(
                                    len(from_category_2),
                                    len(l1_labels),
                                ),
                            },
                            'intersection': len(
                                from_category_1 & from_category_2
                            ),
                        },
                        'cout_elargissement': widening_cost,
                        'labels_sans_release': labels_without_releases,
                    },
                    'a1': {
                        'taille': len(artist_union_by_setting[(k, w)]),
                    },
                }
            )

    if minimum_weight <= 3:
        example_selections = select_label_neighborhood(
            10,
            3,
            label_tier_map,
            prepared_label_neighbors,
        )
        ordered_examples = sorted(
            example_selections,
            key=lambda selection: (
                -selection.weight,
                selection.seed_key,
                selection.label_key,
            ),
        )
        unique_examples: list[LabelSelection] = []
        seen_labels: set[str] = set()
        for selection in ordered_examples:
            if selection.label_key in seen_labels:
                continue
            seen_labels.add(selection.label_key)
            unique_examples.append(selection)
            if len(unique_examples) == 15:
                break

        example_report: dict[str, Any] = {
            'disponible': True,
            'taille_l1': len(
                {selection.label_key for selection in example_selections}
            ),
            'nombre_affiches': len(unique_examples),
            'ordre': (
                'poids décroissant, puis graine et clé ; un label unique par exemple'
            ),
            'exemples': [
                {
                    'cle': selection.label_key,
                    'graine': selection.seed_key,
                    'poids': selection.weight,
                }
                for selection in unique_examples
            ],
        }
    else:
        example_report = {
            'disponible': False,
            'raison': (
                f'Le seuil global est w={minimum_weight} ;Recoverir toutes les '
                'arêtes de poids 3 contredirait le seuil min(ws).'
            ),
        }

    return {
        'uid': uid,
        'parametres': {
            'ks': list(ks),
            'ws': list(ws),
            'seuil_min_recherche_label': minimum_weight,
            'taille_lot_label': BATCH_SIZE,
            'taille_lot_sql': BATCH_SIZE,
            'plafond_par_label': SCORESTORE_PER_LABEL_LIMIT,
        },
        'methode_part_l1': (
            'Labels uniques attribués par catégorie ; les parts peuvent se recouvrir.'
        ),
        'graines': {
            'labels': {
                'total': len(label_tier_map),
                'cat_1': sum(
                    tier == '1' for tier in label_tier_map.values()
                ),
                'cat_2': sum(
                    tier == '2' for tier in label_tier_map.values()
                ),
            },
            'artistes': {
                'total': len(artist_tier_map),
            },
        },
        'poids_aretes_label': {
            'nombre_aretes': len(edge_weights),
            'q50': nearest_rank_quantile(edge_weights, 50),
            'q90': nearest_rank_quantile(edge_weights, 90),
            'q99': nearest_rank_quantile(edge_weights, 99),
            'max': max(edge_weights) if edge_weights else None,
            'methode_quantiles': 'rang le plus proche, ceil(p * n)',
        },
        'combinaisons': combinations,
        'exemples_l1_k10_w3': example_report,
    }


def render_proportion(part: dict[str, Any]) -> str:
    count = part['labels']
    percent = part['pourcent']
    if percent is None:
        return f'{count} (n/a)'
    return f'{count} ({percent:.2f} %)'


def display(value: Any) -> str:
    return 'n/a' if value is None else str(value)


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> list[str]:
    widths = [
        max(
            len(header),
            max((len(row[index]) for row in rows), default=0),
        )
        for index, header in enumerate(headers)
    ]

    def format_row(row: Sequence[str]) -> str:
        cells = [
            value.ljust(width)
            for value, width in zip(row, widths, strict=True)
        ]
        return '| ' + ' | '.join(cells) + ' |'

    separator = '-+-'.join('-' * width for width in widths)
    return [
        format_row(headers),
        separator,
        *(format_row(row) for row in rows),
    ]


def render_text(report: dict[str, Any]) -> str:
    parameters = report['parametres']
    seeds = report['graines']
    distribution = report['poids_aretes_label']
    uid = report['uid']
    ks = ', '.join(str(value) for value in parameters['ks'])
    ws = ', '.join(str(value) for value in parameters['ws'])

    lines = [
        'Largeur des voisinages — diagnostic Découverte',
        f'UID : {uid}',
        (
            f'Réglages : K={ks} ; w={ws} ; '
            f'seuil minimum label={parameters["seuil_min_recherche_label"]} ; '
            f'lots={parameters["taille_lot_label"]}/{parameters["taille_lot_sql"]} ; '
            f'plafond releases par label={parameters["plafond_par_label"]}'
        ),
        (
            f'Graines labels : total={seeds["labels"]["total"]} ; '
            f'cat 1={seeds["labels"]["cat_1"]} ; '
            f'cat 2={seeds["labels"]["cat_2"]}'
        ),
        f'Graines artistes : total={seeds["artistes"]["total"]}',
        (
            f'Poids des arêtes label : n={distribution["nombre_aretes"]} ; '
            f'q50={display(distribution["q50"])} ; '
            f'q90={display(distribution["q90"])} ; '
            f'q99={display(distribution["q99"])} ; '
            f'max={display(distribution["max"])} ; '
            f'méthode={distribution["methode_quantiles"]}'
        ),
        f'Méthode des parts : {report["methode_part_l1"]}',
        '',
    ]

    rows: list[tuple[str, ...]] = []
    for combination in report['combinaisons']:
        l1 = combination['l1']
        provenance = l1['provenance_graines']
        rows.append(
            (
                str(combination['k']),
                str(combination['w']),
                str(l1['taille']),
                render_proportion(provenance['cat_1']),
                render_proportion(provenance['cat_2']),
                str(provenance['intersection']),
                str(l1['cout_elargissement']),
                str(l1['labels_sans_release']),
                str(combination['a1']['taille']),
            )
        )

    lines.extend(
        render_table(
            (
                'K',
                'w',
                'taille L1',
                'part L1 cat 1',
                'part L1 cat 2',
                'cat 1 et 2',
                'coût L1',
                'L1 sans release',
                'taille A1',
            ),
            rows,
        )
    )

    examples = report['exemples_l1_k10_w3']
    lines.append('')
    if not examples['disponible']:
        lines.append(
            'Exemples L1 pour K=10,w=3 : indisponibles. '
            f'Motif : {examples["raison"]}'
        )
    else:
        lines.append(
            f'Exemples L1 pour K=10,w=3 : taille L1={examples["taille_l1"]} ; '
            f'exemples affichés={examples["nombre_affiches"]} ; '
            f'ordre={examples["ordre"]}'
        )
        for item in examples['exemples']:
            cle = item['cle']
            graine = item['graine']
            poids = item['poids']
            lines.append(f'  - clé={cle} ; graine={graine} ; poids={poids}')

    return '\n'.join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Mesure les largeurs L1 et A1 pour plusieurs réglages K et w, '
            'sans écriture ni accès réseau.'
        )
    )
    parser.add_argument(
        '--uid',
        default=paths.DEFAULT_UID,
        help='UID utilisé pour construire le contexte Radar.',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Affiche le rapport JSON au lieu du tableau texte.',
    )
    parser.add_argument(
        '--ks',
        type=parse_nonnegative_ints,
        default='5,10,20',
        metavar='N,N,...',
        help='Valeurs de K séparées par des virgules.',
    )
    parser.add_argument(
        '--ws',
        type=parse_nonnegative_ints,
        default='2,3,5',
        metavar='N,N,...',
        help='Valeurs de poids minimal w séparées par des virgules.',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Ctx(uid=args.uid)

    try:
        report = build_report(
            uid=str(args.uid),
            ctx=ctx,
            ks=tuple(args.ks),
            ws=tuple(args.ws),
        )
    except sqlite3.Error as exc:
        parser.exit(
            status=1,
            message=f'Erreur SQLite en lecture seule : {exc}\n',
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
