# Langfuse: o que cada trace carrega, e como provar que as views funcionam

**Data:** 2026-10-19 · **Status:** implementado
**Repos:** `marketmind-scoring` (níveis, cache por TTL, probe), `marketmind-orchestrator` (level do span raiz, juiz traçado), `infra-manifests` (chaves do scoring)

## O problema

As views **Quality**, **Slow** e **Cost** da página Tracing do Langfuse estavam
vazias. Medido no projeto em 19/10/2026 pela API v2 de observações:

| O que havia nas últimas 100 observações | |
|---|---|
| GENERATION | 0 |
| CHAIN / EVENT / SPAN (nós do orchestrator) | 100 |
| level diferente de DEFAULT | 0 |
| modelo, tokens ou custo | nenhum |

Duas causas, uma por camada:

1. **O scoring não enviava nada.** Toda chamada de modelo acontece nele, e o
   `TracedProvider` (spec de 2026-09-21) já fazia uma generation por chamada —
   mas `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` só estavam no secret do **orchestrator**
   (`orchestrator-external-secret.yaml`) e no `.env` dele. O `.env` e o
   `scoring-external-secret.yaml` do scoring não tinham as chaves. Sem generation
   não há modelo, tokens, custo: a view Cost não tem o que mostrar, e a Slow só
   vê os spans do orchestrator.
2. **Nenhuma falha tratada mudava o level.** O SDK marca ERROR quando uma
   exceção *escapa* do span. Um `give_up` encerra a execução limpamente — level
   DEFAULT — então toda análise que desistiu listava como saudável. No scoring,
   só exceção virava ERROR e nada nunca era WARNING.

Um agravante da UI: desde o SDK 4.7 a tabela abre com o filtro
**"Is Root Observation = true"**. As generations do scoring vivem *dentro* do
trace da análise (W3C `traceparent`), portanto não são raiz. Para vê-las nas
views, remover esse filtro uma vez (o Langfuse lembra a escolha).

## O que cada generation passa a carregar

Mesmo vocabulário em todo lugar, para as views filtrarem igual:

| Campo | Scoring (`traced.py`) | Orchestrator |
|---|---|---|
| `level` ERROR | o provider levantou; `status_message` = `Tipo: mensagem` | `give_up` → span raiz, com o motivo da desistência; exceção que escapa (já era) |
| `level` WARNING | provider não reportou tokens (chamada sem preço); instrução veio do texto do código, não do prompt no Langfuse (generation sem link de versão) | coleta de um país não rodou (transitório); entrega sem narrativa |
| `model` + `usage_details` | `input`, `output`, `input_cache_read`, `input_cache_creation_5m` / `_1h` (preço distinto: 1,25× e 2×) | juiz de narrativa vira generation `narrative_judge` com `input`/`output` |
| `environment` | `LANGFUSE_TRACING_ENVIRONMENT` (lido pelo SDK): `azure-dev` no cluster, `local` na máquina — **igual nos dois serviços**, porque escrevem no mesmo trace | idem |
| `metadata` | `provider`, `site` | `provider`, `site` |

A regra de ouro continua: tracing nunca falha uma chamada nem apaga o motivo de
ela ter falhado (ver `observability.run_span` no orchestrator).

## O probe: um trace padrão por situação

`python scripts/probe_tracing.py` envia seis generations raiz, cada uma no seu
trace, environment `probe`, tag `probe`, sem chamar modelo nenhum:

| Probe | Para a view | Como |
|---|---|---|
| `probe-ok` | nenhuma (linha de base) | DEFAULT, 1.2k in / 300 out, 2 s |
| `probe-warning` | Quality | WARNING + a mensagem real de "sem tokens" |
| `probe-error` | Quality | ERROR + a mensagem real de JSON não parseável |
| `probe-slow` | Slow | span encerrado 90 s depois de aberto (declarado, não esperado) |
| `probe-high-cost` | Cost (> $1) | 190k in + 10k out em `claude-opus-5` = $1,20 pela tabela do próprio Langfuse |
| `probe-high-tokens` | Cost (tokens) | 420k tokens |

Confirmado pela API logo após o envio: os seis chegaram como raiz, com o level,
a latência (90,0 s) e o custo ($1,20 e $2,50) calculados pelo Langfuse. O
`probe-warning` aparece com ~24 tokens: sem `usage_details`, o Langfuse estima
pelo texto — é o que uma chamada real sem usage também vai mostrar.

Quando uma view estiver vazia, o probe separa "não há nada errado" de "nada
está sendo reportado": as duas são a mesma tabela vazia.

## O custo da DataForSEO (segunda rodada, mesmo dia)

Os tokens cobrem os modelos; a demanda vem do Google Ads via DataForSEO, que
cobra **por tarefa** e devolve o valor em cada resposta. Esse valor parava numa
linha de log do coletor (`DATAFORSEO_COST`). Passa a percorrer a mesma cadeia
dos tokens, por header, sem mudar nenhum schema de resposta:

| Serviço | O que faz |
|---|---|
| **coletor** (`marketmind-pytrends`) | `app/core/spend.py`: tally por request (ContextVar; o cliente roda em `to_thread`, que copia o contexto). `_extract_results` soma `task.cost` **antes** de checar o status, porque tarefa falhada também é cobrada. Middleware põe `X-DataForSEO-Cost-USD`, `X-DataForSEO-Calls` e `X-DataForSEO-Calls-Without-Cost` em toda resposta, inclusive erro. |
| **scoring** | `collection_client.dispatch` vira uma observação `collect <fonte>` (tipo `tool`: latência, ERROR na falha) no trace da análise. Quando a resposta traz os headers, grava um filho `dataforseo` (tipo generation, modelo `dataforseo/google-ads`, `cost_details={"total": usd}`, `usage_details={"tasks": n}`) — o SDK só carrega custo em tipos generation-like, e o Langfuse aceita `cost_details` como dado, sem tabela de preço. `demand_spend.py` soma o request; o middleware repassa os mesmos headers ao orchestrator. |
| **orchestrator** | `spend.add_response` lê os headers (`dataforseo_usd`, `dataforseo_calls`); `metrics.demand_cost()` grava o score `dataforseo_cost_usd` no fim da execução, ao lado de `token_consumption`. |

Resultado: o custo total de uma análise no Langfuse passa a incluir a demanda,
cada coleta aparece com o que custou, e as outras três fontes (World Bank,
OECD, UN Data) continuam sem custo e sem header.

## O custo total, em dólares, no log (terceira rodada, mesmo dia)

Até aqui o único lugar com o total em dólares era o Langfuse, porque só ele
tinha a tabela de preços. Em vez de copiar a tabela para o código (desatualiza
em silêncio a cada modelo novo), cada serviço **lê a tabela do próprio
Langfuse** ao subir (`api.models.list`, paginada; definições do projeto vencem
as gerenciadas, como o Langfuse faz) e precifica a chamada no momento em que
ela acontece. `app/pricing.py` nos dois serviços; o scoring soma no
`llm_usage` e manda `X-LLM-Cost-USD`, `X-LLM-Cost-By-Model` e
`X-LLM-Calls-Without-Cost`; o orchestrator precifica o juiz e soma tudo.

No fim de cada execução, uma linha e um score:

```
COST analysis=<id> total_usd=1.6355 models_usd=0.5555 (claude-opus-5 0.5091, gpt-5.1 0.0464) dataforseo_usd=1.0800 (12 tasks)
```

Score `analysis_cost_usd`, ao lado de `token_consumption` e `dataforseo_cost_usd`.
Chamada que não dá para precificar (tabela ainda não lida, modelo
desconhecido, provider sem usage) é **contada, não zerada**: a linha ganha
`unpriced_calls=N (the total is a floor)`. Langfuse fora do ar no startup: o
serviço sobe sem preços e tenta de novo a cada 60 s.

## Entrega

- Scoring: `app/services/llm/traced.py`, `app/prompts.py` (`take_fallback_reason`),
  `app/services/llm/usage.py` (cache por TTL), `scripts/probe_tracing.py`,
  testes em `tests/test_llm_tracing.py` e `tests/test_probe_tracing.py`.
- Orchestrator: `observability.flag`, usada em `give_up_node`, na coleta e na
  narração; juiz traçado em `quality.judge_support`.
- Coletor: `app/core/spend.py`, header middleware em `app/main.py`, soma em
  `dataforseo_client._extract_results`; testes em `tests/test_spend.py`.
- Scoring (custo): `app/services/demand_spend.py`, `observability.observe`/`Observation.record`,
  `collection_client.dispatch`; testes em `tests/test_demand_spend.py`.
- Orchestrator (custo): `spend.Spend.dataforseo_*`, `metrics.demand_cost`.
- Custo total: `app/pricing.py` (scoring e orchestrator), `usage.charge`, headers
  `X-LLM-Cost-*`, `spend.charge_model`, `metrics.analysis_cost`; testes em
  `tests/test_pricing.py` dos dois lados.
- Infra: chaves do Langfuse no `scoring-external-secret.yaml` (mesmas entradas
  do vault), `LANGFUSE_HOST` e `LANGFUSE_TRACING_ENVIRONMENT=azure-dev` nos dois
  `values.yaml`. Entra em vigor no dev depois de commit + sync do ArgoCD.
