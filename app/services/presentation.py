"""Como o resultado de uma análise é mostrado: quem foi pedido, quem é o
campeão, em que ordem os países aparecem e qual é o destaque.

Era decidido em três lugares. O orquestrador marcava `requested` e
`is_champion` nas linhas, o frontend ordenava e escolhia o destaque, e o pacote
de evidências reconstruído na leitura saía sem marca nenhuma. Os três
precisavam concordar e nada os obrigava a isso; agora as linhas, o pacote e a
resposta de status leem as mesmas funções.
"""

from typing import Any

from app.services.mos.interpretation import mos_presentation


def _mos(row: dict[str, Any]) -> float | None:
    valor = row.get("mos")
    return None if valor is None else float(valor)


def champion_of(rows: list[dict[str, Any]], requested: list[str]) -> str | None:
    """O país coroado entre as linhas entregues: o de maior MOS, e no empate o
    pedido. É a mesma regra de AnalysisService.crown_champion, que põe a linha
    do campeão fora do pedido entre as entregues — então o maior MOS delas É o
    campeão que foi coroado e guardado."""
    pedidos = set(requested)
    pontuadas = [r for r in rows if _mos(r) is not None]
    # Os pedidos primeiro: `max` devolve o primeiro dos empatados.
    pontuadas.sort(key=lambda r: r.get("country") not in pedidos)
    melhor = max(pontuadas, key=lambda r: _mos(r) or 0.0, default=None)
    return None if melhor is None else melhor.get("country")


def display_order(rows: list[dict[str, Any]], requested: list[str]) -> list[str]:
    """A ordem em que os países aparecem: o campeão fora do pedido primeiro,
    depois os pedidos pelo MOS, os sem MOS por último.

    O campeão não pedido não é uma posição do ranking: é a resposta a outra
    pergunta ("o melhor mercado do produto, que você não pediu"). Ordenado só
    pelo MOS, um campeão de 66 caía entre o #1 pedido a 69 e o #2 a 42, como um
    cartão sem número no meio da lista.
    """
    pedidos = set(requested)

    def chave(row: dict[str, Any]) -> tuple[int, int, float]:
        mos = _mos(row)
        return (0 if row.get("country") not in pedidos else 1, 1 if mos is None else 0, -(mos or 0.0))

    return [str(r.get("country")) for r in sorted(rows, key=chave)]


def headline_country(rows: list[dict[str, Any]], requested: list[str]) -> str | None:
    """O país do destaque: o melhor PEDIDO, para que a escolha de quem pediu
    continue sendo a manchete; sem pedido pontuado, o melhor pontuado."""
    pedidos = set(requested)
    ordem = display_order(rows, requested)
    por_pais = {r.get("country"): r for r in rows}
    pontuados = [c for c in ordem if _mos(por_pais[c]) is not None]
    return next((c for c in pontuados if c in pedidos), pontuados[0] if pontuados else None)


def present_rows(rows: list[dict[str, Any]], *, requested: list[str], language: str) -> list[dict[str, Any]]:
    """Cada linha com o que se mostra dela: `requested`, `is_champion`,
    `mos_display`, `mos_band_key` e `mos_band_label` (ver mos_presentation).
    Idempotente — reaplicar a uma linha já apresentada dá a mesma linha."""
    pedidos = set(requested)
    campeao = champion_of(rows, requested)
    return [
        {
            **row,
            "requested": row.get("country") in pedidos,
            "is_champion": campeao is not None and row.get("country") == campeao,
            **mos_presentation(_mos(row), language),
        }
        for row in rows
    ]
