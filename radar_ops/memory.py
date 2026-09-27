"""Sonde de la mémoire de l'hôte, en lecture seule, pour le cadran temps réel.

`radar_ops` vit dans son propre conteneur : il ne voit pas les autres conteneurs
de l'application. En revanche /proc/meminfo n'est pas isolé par Docker tant
qu'aucune limite mémoire n'est posée (cf. docker-compose.yml) : il décrit donc
la mémoire de l'HÔTE — le VPS entier — ce qui est exactement la « capacité du
serveur » que la page /serveur veut montrer.

Aucune dépendance externe (pas de psutil) : le format de /proc/meminfo est
stable et la lecture d'un fichier suffit. Comme le reste du diagnostic, ce
module ne lève jamais : il préfère un instantané marqué indisponible à une page
qui tombe.
"""
import time


MEMINFO_PATH = '/proc/meminfo'


def _kb_to_bytes(kb: int) -> int:
    """Convertit des kilo-octets (kB) en octets.
    Pourquoi : /proc/meminfo exprime les tailles en kB ; l'API interne utilise des octets."""
    return kb * 1024


def read_meminfo(path: str = MEMINFO_PATH) -> dict[str, int]:
    """Lit /proc/meminfo et renvoie un dict {clé: valeur_en_octets}.
    Pourquoi : lecture seule, sans effet de bord, résiliente aux lignes malformées.
    Ignore les lignes sans ':' ou sans entier parsable. Ne lève jamais."""
    data: dict[str, int] = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if ':' not in line:
                    continue
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip()
                # Format attendu: "12345 kB"
                parts = val.split()
                if not parts:
                    continue
                try:
                    kb = int(parts[0])
                except ValueError:
                    continue
                data[key] = _kb_to_bytes(kb)
    except OSError:
        # Fichier absent ou illisible (ex: non-Linux) -> dict vide
        pass
    return data


def snapshot(path: str = MEMINFO_PATH) -> dict:
    """Renvoie un instantané mémoire sérialisable en JSON.
    Pourquoi : format stable pour cadran temps réel, avec drapeaux de disponibilité.
    Ne lève jamais ; en cas d'échec, 'available'=False et 'reason' explicite."""
    ts = time.time()
    mem = read_meminfo(path)

    # Valeurs par défaut pour instantané dégradé
    result = {
        'available': False,
        'ts': ts,
        'total_b': None,
        'available_b': None,
        'used_b': None,
        'used_pct': None,
        'free_b': None,
        'buffers_b': None,
        'cached_b': None,
        'swap_total_b': None,
        'swap_used_b': None,
        'swap_pct': None,
        'reason': None,
    }

    total_b = mem.get('MemTotal')
    if not total_b:
        result['reason'] = 'MemTotal absent ou nul dans /proc/meminfo'
        return result

    # Mémoire principale
    avail_b = mem.get('MemAvailable')
    free_b = mem.get('MemFree')
    buffers_b = mem.get('Buffers')
    cached_b = mem.get('Cached')

    if avail_b is not None:
        used_b = max(0, total_b - avail_b)
    elif all(v is not None for v in (free_b, buffers_b, cached_b)):
        # Repli noyau ancien : disponible ≈ free + buffers + cached
        used_b = max(0, total_b - (free_b + buffers_b + cached_b))
        avail_b = total_b - used_b
    else:
        result['reason'] = 'Impossible de calculer la mémoire utilisée (clés manquantes)'
        return result

    used_pct = round(used_b / total_b * 100, 1) if total_b > 0 else None

    # Swap
    swap_total_b = mem.get('SwapTotal')
    swap_free_b = mem.get('SwapFree')
    swap_used_b = None
    swap_pct = None
    if swap_total_b is not None and swap_free_b is not None:
        swap_used_b = max(0, swap_total_b - swap_free_b)
        swap_pct = round(swap_used_b / swap_total_b * 100, 1) if swap_total_b > 0 else 0.0

    result.update({
        'available': True,
        'total_b': total_b,
        'available_b': avail_b,
        'used_b': used_b,
        'used_pct': used_pct,
        'free_b': free_b,
        'buffers_b': buffers_b,
        'cached_b': cached_b,
        'swap_total_b': swap_total_b,
        'swap_used_b': swap_used_b,
        'swap_pct': swap_pct,
        'reason': None,
    })
    return result
