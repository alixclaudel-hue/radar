"""Tokens équivalents : un seul volume comparable entre Claude et le courtier.

Un jeton de sortie coûte nettement plus cher à produire qu'un jeton de cache
relu, et un jeton d'entrée frais plus cher qu'un jeton de cache relu mais
moins qu'un jeton de cache écrit. Additionner des jetons bruts de nature
différente (ce que fait `total_tokens` un peu partout dans ce paquet) mélange
des grandeurs qui ne se valent pas — les « tokens équivalents » les ramènent
tous au coût d'un jeton d'entrée frais, pour comparer un VOLUME de travail,
pas un coût réel : `cost_usd`, calculé ailleurs à partir des vrais prix par
modèle, reste la seule mesure de coût. Poids volontairement identiques pour
tous les modèles (décision utilisateur) : on compare la charge de travail
traitée, jamais ce qu'elle a coûté en dollars.
"""

from __future__ import annotations

WEIGHTS: dict[str, float] = {
    "input": 1.0,
    "cache_read": 0.10,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.0,
    "output": 5.0,
}

# Repli quand une source ne distingue pas lecture et écriture de cache (reçus
# du courtier avant instrumentation, par exemple) : une moyenne prudente entre
# les deux, jamais un vrai poids mesuré.
CACHE_AGG: float = 0.15


def equiv(
    input: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output: int = 0,
    cache_write_1h: int | None = None,
    cache_write_5m: int | None = None,
    cache_agg: int | None = None,
) -> int:
    """Volume de jetons ramené à un seul nombre, en équivalent-jetons-d'entrée.

    `cache_agg` prime sur `cache_read`/`cache_write` : un appelant qui ne sait
    pas ventiler le cache ne doit pas se faire imposer un poids de lecture ou
    d'écriture qu'il n'a pas mesuré. Pour l'écriture de cache, `cache_write_1h`
    / `cache_write_5m` (champs récents, absents des anciennes lignes) priment
    sur `cache_write` : quand l'un des deux est fourni (même à 0), le reliquat
    non ventilé de `cache_write` est compté au tarif 1 h — les sessions Claude
    Code de ce dépôt utilisent le cache 1 h (décision utilisateur 27/09), donc
    un jeton d'écriture non identifié est plus probablement un 1 h qu'un 5 m.
    Sans aucune ventilation, tout `cache_write` est compté au tarif 1 h pour la
    même raison."""
    input_v = input or 0
    output_v = output or 0

    if cache_agg is not None:
        total = (input_v * WEIGHTS["input"]
                 + cache_agg * CACHE_AGG
                 + output_v * WEIGHTS["output"])
        return round(total)

    cache_read_v = cache_read or 0

    if cache_write_1h is not None or cache_write_5m is not None:
        write_1h = cache_write_1h or 0
        write_5m = cache_write_5m or 0
        reste = (cache_write or 0) - (write_1h + write_5m)
        if reste < 0:
            reste = 0
        cout_ecriture = (write_1h * WEIGHTS["cache_write_1h"]
                          + write_5m * WEIGHTS["cache_write_5m"]
                          + reste * WEIGHTS["cache_write_1h"])
    else:
        cout_ecriture = (cache_write or 0) * WEIGHTS["cache_write_1h"]

    total = (input_v * WEIGHTS["input"]
             + cache_read_v * WEIGHTS["cache_read"]
             + cout_ecriture
             + output_v * WEIGHTS["output"])
    return round(total)


def weights_public() -> dict[str, float]:
    """Copie des poids, sérialisable telle quelle pour le JS du gabarit."""
    return dict(WEIGHTS)
