import re
import unicodedata
from collections.abc import Sequence


def normalize_alias(text: str) -> str:
    r"""Deterministic text normalization for product_aliases.alias_normalized
    (catalog_service pipeline step 1 — exact alias lookup). Same input text
    must always normalize to the same key: lowercase, accents stripped,
    punctuation removed, whitespace collapsed.

    MANTÉM ESCRITA NÃO-LATINA (2026-08-25). Até então a última substituição era
    `[^a-z0-9\s]`, que apagava tudo fora do alfabeto inglês — e um texto
    inteiramente em japonês, chinês, coreano, russo ou árabe normalizava para
    string VAZIA. Duas consequências, ambas medidas no catálogo real:

      1. Corrupção silenciosa. `resolve()` gravava o alias vazio da primeira
         entidade criada assim, e QUALQUER texto não-latino seguinte casava
         com ele pelo passo 1. Reproduzido: 'ノンフライヤー' (air fryer),
         '空气炸锅' (air fryer) e 'Пылесос-робот' (aspirador robô) viravam a
         MESMA entidade — e por ser o caminho O(1), o veto do passo 2 nem
         chegava a rodar.
      2. Perda de alcance. 46 dos termos curados do catálogo, em 5 mercados
         (JPN, CHN, KOR, RUS, SAU), não tinham representação e nunca viravam
         alias.

    `[\W_]` é a mesma regra com consciência de Unicode: continua tirando
    pontuação, símbolos e emoji, e passa a preservar letra e dígito de
    qualquer escrita. Para texto latino o resultado é idêntico ao anterior,
    com duas exceções que ANTES perdiam informação: 'ß' e 'ł' não têm
    decomposição NFKD e viravam espaço ('heißluftfritteuse' -> 'hei
    luftfritteuse'). Os aliases já gravados com a forma antiga continuam
    válidos e apontando para a mesma entidade; o backfill apenas acrescenta a
    forma correta ao lado.

    Ainda pode devolver vazio — para um texto só de pontuação ou emoji. Quem
    grava alias trata isso explicitamente (ver catalog_service._gravar_alias e
    a CHECK da migration 028): vazio nunca vira alias.

    O NFC no fim recompõe o que o NFKD separou e o passo de acentos não
    removeu. Importa para o coreano: o NFKD quebra cada sílaba hangul em jamo,
    e sem recompor o alias ficaria gravado numa forma que ninguém digita —
    consistente, mas ilegível em log e em consulta manual ao banco. Para o
    latino não muda nada: depois de tirar os combinantes não sobra o que
    recompor.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.lower()
    sem_pontuacao = re.sub(r"[\W_]", " ", lowered, flags=re.UNICODE)
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", sem_pontuacao).strip())


def as_term_list(value: object) -> list[str]:
    """Coerce a localized_terms value to a clean list of terms, tolerating the
    legacy `{lang: "term"}` (single string) shape alongside `{lang: [terms]}`."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [t for t in value if isinstance(t, str) and t.strip()]
    return []
