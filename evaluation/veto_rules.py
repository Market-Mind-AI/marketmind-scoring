"""Regras de decisão do veto antagônico, sobre os mesmos vetores, para a ablação.

Entrada de todas: a consulta `q`, os termos do produto `pos` (n_p x d) e os
antagônicos `ant` (n_a x d), todos normalizados em L2 — coseno vira produto
escalar. Saída: um ESCORE com sinal. Positivo = lado do produto, negativo =
lado do antagônico, e o módulo é a margem, que a classificação seletiva usa
como confiança.

  nn        1-vizinho relativo: max sim(q, pos) - max sim(q, ant). A regra que entrou
            em produção primeiro. Enviesada contra o veto quando n_p >> n_a: o
            máximo sobre mais pontos tende a ser maior por acaso.
  knn       média dos k melhores por lado. Amortece o acaso de um ponto só.
  centroid  centroide mais próximo (Rocchio como classificador, Manning et al.
            IIR §14.2): sim(q, mu_P) - sim(q, mu_A).
  axis      projeção no EIXO de Rocchio: ((q - m) . (mu_P - mu_A)/|.|), com m o
            ponto médio. A subtração cancela o que os dois lados compartilham
            (o nome do produto, "best", "price", o idioma) e deixa só a direção
            do atributo definidor — ar x óleo. Caso particular do LDA de Fisher
            com covariância identidade.
  lda       LDA de Fisher com shrinkage: w = (cov + lambda I)^-1 (mu_P - mu_A),
            cov a covariância intra-classe agrupada. Com dezenas de pontos em
            1024 dimensões, cov é singular; lambda = tr(cov)/d a leva para o eixo.
"""

from collections.abc import Callable

import numpy as np

REGRAS: dict[str, Callable[[np.ndarray, np.ndarray, np.ndarray], float]] = {}


def _regra(nome: str) -> Callable[..., Callable[..., float]]:
    def registra(f: Callable[[np.ndarray, np.ndarray, np.ndarray], float]) -> Callable[..., float]:
        REGRAS[nome] = f
        return f

    return registra


def normalizar(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.where(n == 0, 1.0, n)


@_regra("nn")
def nn(q: np.ndarray, pos: np.ndarray, ant: np.ndarray) -> float:
    return float((pos @ q).max() - (ant @ q).max())


@_regra("knn")
def knn(q: np.ndarray, pos: np.ndarray, ant: np.ndarray, k: int = 3) -> float:
    def topo(s: np.ndarray) -> float:
        return float(np.sort(s)[-min(k, len(s)) :].mean())

    return topo(pos @ q) - topo(ant @ q)


@_regra("centroid")
def centroid(q: np.ndarray, pos: np.ndarray, ant: np.ndarray) -> float:
    return float(q @ normalizar(pos.mean(0)) - q @ normalizar(ant.mean(0)))


@_regra("axis")
def axis(q: np.ndarray, pos: np.ndarray, ant: np.ndarray) -> float:
    mu_p, mu_a = pos.mean(0), ant.mean(0)
    eixo = normalizar(mu_p - mu_a)
    return float((q - (mu_p + mu_a) / 2) @ eixo)


@_regra("lda")
def lda(q: np.ndarray, pos: np.ndarray, ant: np.ndarray) -> float:
    mu_p, mu_a = pos.mean(0), ant.mean(0)
    desvios = np.vstack([pos - mu_p, ant - mu_a])
    cov = desvios.T @ desvios / max(len(desvios) - 2, 1)
    d = cov.shape[0]
    lam = np.trace(cov) / d
    w = np.linalg.solve(cov + lam * np.eye(d), mu_p - mu_a)
    w = w / np.linalg.norm(w)
    return float((q - (mu_p + mu_a) / 2) @ w)


def pureza_leave_one_out(pos: np.ndarray, ant: np.ndarray) -> int:
    """Quantos antagônicos são INCONSISTENTES com o resto do próprio conjunto:
    tirando um de cada vez, o eixo feito com os demais o projeta no lado do
    PRODUTO. Um `oat milk` entre os antagônicos de `lactose-free milk` cai
    aqui — também não tem lactose, está do lado errado do atributo. É um
    indício de lista poluída (vizinho de categoria em vez de oposto), não uma
    prova: com poucos antagônicos o eixo dos demais é instável."""
    if len(ant) < 3:
        return 0
    inconsistentes = 0
    for i in range(len(ant)):
        resto = np.delete(ant, i, axis=0)
        if axis(ant[i], pos, resto) > 0:
            inconsistentes += 1
    return inconsistentes


def classificacao_seletiva(escores: np.ndarray, gabarito: np.ndarray) -> list[tuple[float, float, float]]:
    """(limiar de abstenção, cobertura, acurácia entre os decididos), para cada
    limiar que muda alguma coisa. `gabarito` = +1 produto, -1 antagônico.
    Abster-se é mandar ao LLM: a acurácia aqui é só do que o vetor decide."""
    margens = np.abs(escores)
    acertos = np.sign(escores) == gabarito
    curva = []
    for t in np.unique(np.concatenate([[0.0], margens])):
        decididos = margens > t
        if not decididos.any():
            break
        curva.append((float(t), float(decididos.mean()), float(acertos[decididos].mean())))
    return curva
