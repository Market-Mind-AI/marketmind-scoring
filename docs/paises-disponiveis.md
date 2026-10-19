> **SUPERADO / HISTÓRICO (2026-10-19) — ver [marketmind-arquitetura.md](marketmind-arquitetura.md) e [marketmind-transparencia-tecnica.md](marketmind-transparencia-tecnica.md).**
>
> A lista fixa de 24 países abaixo foi substituída por uma cesta DINÂMICA:
>
> - Migration 043 (2026-09-23) carregou 127 países MEDIDOS contra as APIs (World Bank 12 indicadores, UN Data 4, DataForSEO catálogo de localizações) com `analysis_ready = false`; o pré-aquecimento macro (`POST /admin/coverage/prewarm-macro`) os promove quando o dado chega.
> - Migration 054 (2026-10-14) exclui China, Rússia e Belarus: o coletor Google Ads os recusa, e nada parcial é entregue.
> - `GET /countries` devolve só os `analysis_ready`; a coluna `search_language` fixa por país deixou de ser a fonte do idioma — ele vem do `localized_markets[ISO3]` do produto.
> - Não existe mais 'rate-limit do Google Trends': a demanda é volume absoluto do Google Ads via DataForSEO.
> - A arquitetura de dados e o que a regra 'nada parcial' implica estão em marketmind-arquitetura.md.

# Países disponíveis para análise

Este documento define **quais países o MarketMind aceita numa análise** e
**por que não são todos os países do mundo**. A restrição é uma consequência
direta das limitações das fontes de dados (APIs), não uma escolha arbitrária.

## A lista (24 países)

O cesto de referência é um conjunto **curado** de grandes economias com dados
confiáveis nas quatro fontes do pipeline, agrupado por continente e em ordem
alfabética (a mesma ordem devolvida por `GET /countries`):

| Continente | Países (ISO3) |
|---|---|
| Africa | ZAF |
| Americas | ARG, BRA, CAN, CHL, COL, MEX, PER, USA |
| Asia | IDN, IND, JPN, SAU, TUR |
| Europe | DEU, ESP, FRA, GBR, ITA, NLD, POL, PRT, SWE |
| Oceania | AUS |

**Onde isso é aplicado (fontes da verdade):**
- Frontend (seletor de mercados): `marketmind-ai/web/src/lib/api.ts` → `COUNTRIES`.
- Coletor de demanda: `marketmind-pytrends/app/clients/dataforseo_locations.py` → `ISO2_TO_LOCATION_NAME` (suportados) e `UNSUPPORTED_ISO2` (excluídos, com motivo).
- Cesto de referência macro: seed `alembic/versions/003_seed_reference_countries.py`.

## Por que não todos os países do mundo?

O MOS combina quatro fontes; a análise só roda para um país se **todas** as
métricas obrigatórias existirem para ele (gate de completude — nunca calculamos
um MOS sobre dados parciais). Cada fonte impõe um limite:

### 1. Demanda (Google Ads via DataForSEO) — o limite mais forte
A dimensão de maior peso (Demand Volume, ~0,386) vem do **volume real de buscas
do Google Ads**. Isso só é representativo onde **o Google é o buscador dominante**
e onde o **DataForSEO serve a localização**. Consequências:

- **Rússia (RUS) e Belarus (BLR): excluídos.** O DataForSEO não atende esses
  países em nenhum de seus serviços (sanções). Qualquer chamada falha
  explicitamente.
- **China (CHN): excluída.** O Google é bloqueado na China continental (o Baidu
  domina), então o Google Ads não retorna volume de busca representativo.
- **Coreia do Sul (KOR): excluída.** O Naver detém ~55-60% das buscas, então o
  Google Ads vê só a fatia do Google e **subestima materialmente** a demanda
  real — excluída até integrarmos uma fonte Naver.

RUS e CHN ficam no `UNSUPPORTED_ISO2` do coletor (cada um com seu motivo); KOR
foi removida do cesto (`countries`) e do seletor. Todas fora do frontend.

### 2. Cobertura de buscadores locais (caveat do incluído)
O **Japão (JPN)** permanece incluído com uma ressalva menor: o Yahoo! Japan tem
participação relevante, mas **usa o motor de busca do Google**, então o impacto
no volume medido é pequeno. É o único caso-limite que mantivemos.

### 3. Dados macro (World Bank / OECD / UN)
Muitos países (sobretudo pequenos ou de baixa renda) têm **lacunas** nos
indicadores macro (PIB, inflação, penetração digital, demografia…). Sem o
conjunto obrigatório completo, o gate de completude excluiria o país — então
eles não entram no cesto. A OCDE, em particular, não cobre todos os países (por
isso `real_estate_growth` é opcional).

### 4. Significância do sinal
Mercados muito pequenos produzem volumes de busca **esparsos e ruidosos**, sem
significância estatística para um score de oportunidade confiável. O cesto
prioriza economias com massa de dados suficiente.

## Roadmap para ampliar o cesto

A forma correta de incluir mercados hoje fora (RUS, CHN, e melhorar KOR) é
**integrar fontes de demanda por país**, normalizadas para o mesmo "volume
mensal" que alimenta a régua:

- **Coreia:** Naver Search Ad API (volume de keyword) / Naver DataLab.
- **Rússia:** Yandex Wordstat via Yandex Direct API.
- **China:** Baidu Keyword Planner / Baidu Index + sinais de e-commerce (Taobao/Tmall, JD).
- **Universal:** Amazon (já disponível via DataForSEO) e marketplaces locais como
  proxy de intenção de compra onde o Google é fraco.

O coletor é isolado por fonte (um serviço por fonte), então cada adaptador novo
encaixa sem alterar o motor de MOS.
