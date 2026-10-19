"""Compara el MOS de producción con el método clásico guardado al lado.

Cada fila de `mos_results` lleva dos notas: `mos` (Rank Reciprocal + TOPSIS con
anclas fijas 100/0, calculado país a país) y `library_mos` (entropía + TOPSIS
clásico de `pymcdm`, relativo a los países de la misma consulta). Este script
agrupa las filas por producto y periodo, se queda con los grupos de tres o más
países y mide si los dos métodos ordenan igual: correlación de Spearman entre
las dos notas dentro de cada grupo, porcentaje de grupos que señalan el mismo
mejor país y diferencia media de nivel.

Solo lee. Usa la misma configuración de base de datos que la aplicación:

    python scripts/compare_with_classical.py
"""

import asyncio
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402

MIN_PAISES = 3


def _rangos(valores: list[float]) -> list[float]:
    orden = sorted(range(len(valores)), key=lambda i: valores[i])
    rangos = [0.0] * len(valores)
    i = 0
    while i < len(orden):
        j = i
        while j + 1 < len(orden) and valores[orden[j + 1]] == valores[orden[i]]:
            j += 1
        for k in range(i, j + 1):
            rangos[orden[k]] = (i + j) / 2 + 1
        i = j + 1
    return rangos


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _rangos(a), _rangos(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


async def main() -> None:
    async with async_session_factory() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT entity_key, period_key, country, mos, library_mos "
                    "FROM mos_results "
                    "WHERE mos IS NOT NULL AND library_mos IS NOT NULL "
                    "AND lower(availability_status) = 'available'"
                )
            )
        ).all()

    grupos: dict[tuple[str, str], list[tuple[str, float, float]]] = defaultdict(list)
    for entity_key, period_key, country, mos, library_mos in filas:
        grupos[(str(entity_key), period_key)].append((country, float(mos), float(library_mos)))

    rhos: list[float] = []
    diferencias: list[float] = []
    mismo_mejor = 0
    tamanos: list[int] = []
    for paises in grupos.values():
        if len(paises) < MIN_PAISES:
            continue
        tamanos.append(len(paises))
        nuestras = [p[1] for p in paises]
        clasicas = [p[2] for p in paises]
        rho = spearman(nuestras, clasicas)
        if rho == rho:
            rhos.append(rho)
        diferencias += [abs(a - b) for a, b in zip(nuestras, clasicas, strict=True)]
        if max(paises, key=lambda p: p[1])[0] == max(paises, key=lambda p: p[2])[0]:
            mismo_mejor += 1

    print(f"filas con las dos notas: {len(filas)}")
    if not tamanos:
        print(f"ningún grupo producto×periodo con {MIN_PAISES} países o más")
        return
    print(
        f"grupos producto×periodo con >= {MIN_PAISES} países: {len(tamanos)} "
        f"(mediana {statistics.median(tamanos):.0f} países, máximo {max(tamanos)})"
    )
    print(
        f"spearman: media {statistics.mean(rhos):.2f} · mediana {statistics.median(rhos):.2f} · "
        f"mínimo {min(rhos):.2f} · máximo {max(rhos):.2f}"
    )
    print(f"grupos con rho >= 0,8: {100 * sum(1 for r in rhos if r >= 0.8) / len(rhos):.0f} %")
    print(f"mismo mejor país: {100 * mismo_mejor / len(tamanos):.0f} %")
    print(
        f"diferencia de nivel |mos - library_mos|: media {statistics.mean(diferencias):.1f} puntos · "
        f"mediana {statistics.median(diferencias):.1f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
