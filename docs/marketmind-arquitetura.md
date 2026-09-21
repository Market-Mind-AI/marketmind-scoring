# MarketMind — arquitetura

Estado real do sistema em 21/09/2026. Substitui `marketmind-implementation-plan.md`
e `marketmind-arquitetura-tecnica-python.md`, que descrevem a orquestração
anterior.

---

## 1. O que o sistema responde

Uma pergunta só: **em quais mercados vale a pena vender este produto, e por quê.**

O usuário descreve um produto (texto ou foto) e escolhe países. O sistema
devolve uma pontuação por país (MOS — *Market Opportunity Score*), o detalhe de
como cada nota foi formada, e um texto de negócio explicando o resultado.

**A invariante que sustenta tudo:** *o modelo de linguagem nunca calcula um
número.* Ele nomeia o produto no idioma de cada país, decide o que não é aquele
produto, e escreve a explicação no fim. Todo valor numérico vem de dado coletado
e de fórmula determinística. É isso que torna possível verificar a explicação
contra a evidência — e é o argumento central da defesa.

---

## 2. Os seis serviços

| serviço | de quem é o dado | papel |
|---|---|---|
| **marketmind-ai** (web) | — | a tela. Build estático (Astro), sem servidor próprio |
| **marketmind-orchestrator** | a execução da análise | **o agente.** Porta de entrada e cérebro |
| **marketmind-scoring** | catálogo, indicadores, MOS, registro da análise | o método |
| **marketmind-pytrends** | volume de busca (Google Ads via DataForSEO) | coleta |
| **marketmind-worldbank** | indicadores macro anuais | coleta |
| **marketmind-oecd** | indicadores macro anuais | coleta |
| **marketmind-united-nations-data** | demografia | coleta |

O navegador fala **só com o agente**, em `/api`. Nenhum outro serviço é
alcançável pelo navegador.

```
navegador ──/api──▶ orchestrator (agente)
                        │
                        ├── ferramentas ──▶ scoring  (catálogo, indicadores, MOS)
                        │                       │
                        │                       └──▶ os 4 serviços de coleta
                        └── checkpointer Postgres (onde a execução parou)
```

---

## 3. O agente

Um grafo LangGraph. Cada nó é um passo; a única ramificação real é a decisão da
análise.

| nó | o que faz |
|---|---|
| `start` | lê o que já foi decidido — não redecide nada |
| `enrich` | dá ao produto um vocabulário de busca em cada país |
| `collect` | manda coletar o que falta, por país |
| `ruler` | fixa a régua de demanda (o mercado de referência) |
| `measure` | calcula os 22 indicadores |
| `score` | calcula o MOS |
| `champion` | coroa o melhor mercado do período |
| `evidence` | monta o pacote de evidência |
| `deliver` | **entrega a resposta** |
| `narrate` | escreve o texto, depois |
| `give_up` | encerra com motivo |

**Por que `deliver` vem antes de `narrate`:** as notas são a resposta; o texto é
uma leitura delas. Uma chamada de modelo já demorou mais que toda a análise, e
prender a entrega a ela faz o usuário esperar pelo enfeite.

**Por que nada parcial sai:** um país faltando num ranking não é uma resposta
menor, é uma resposta diferente. Se um país não pontuou, o agente insiste
enquanto a causa for fonte fora do ar (até 24 h), e desiste quando a fonte
simplesmente não tem aquele dado — insistir nisso nunca termina.

### Estado durável

O grafo grava em um checkpointer Postgres, com `thread_id` = `analysis_id`.
Reinvocar é **retomar**, nunca começar de novo.

O checkpoint guarda **referências, não dados** — identificadores, quais países
já saíram, qual régua vale. Os indicadores e o pacote de evidência vivem no
scoring; duplicá-los criaria duas verdades sobre a mesma análise.

**Um checkpointer não é agendador.** Ele não se retoma sozinho. A execução é uma
task no agente e morre com o pod; quem a traz de volta é a varredura do scoring,
que procura análises `running` caladas há mais de 15 minutos e reinvoca pelo
mesmo `analysis_id`. Verificado em produção: `Retomando análise …, parada há
mais de 900s`.

---

## 4. O método (o que o agente não decide)

**22 indicadores em 7 dimensões:** volume de demanda, qualidade da demanda,
maturidade digital, maturidade econômica, tamanho de mercado, sustentabilidade
demográfica e estrutura macro da demanda.

**Pesos por Rank Reciprocal**, a partir de uma ordem de prioridade explícita —
não por variância estatística. Trocado de Entropy Weight porque variância não é
importância: um indicador que varia muito entre países não é, por isso, mais
decisivo.

**Agregação por TOPSIS**, por país, nunca entre países.

O MOS é um **ranking relativo**, não uma nota absoluta. 55 não significa "bom";
significa "melhor que os outros desta comparação".

### O que faz a demanda ser confiável

Três regras que custaram caro para acertar:

- **Nada de teto na expansão.** Havia um limite de 15 keywords relacionadas. O
  Google devolveu 2 127 para picanha no Brasil e 1 967 passaram por todos os
  filtros — o teto guardava 15, descartando `picanha swift` e `picanha bassi`,
  que são as marcas que as pessoas compram. Um teto sobre uma soma é um teto
  sobre a medição.
- **Variantes próximas contam uma vez.** O Google reporta um mesmo volume sob
  várias grafias. Mas compartilhar *o nome do produto* não é prova de ser a
  mesma busca — o filtro de relevância garante que toda keyword o carrega. É
  preciso: mesmas palavras em outra ordem, palavras de uma contidas na outra,
  uma palavra em comum **fora** do vocabulário do produto, ou strings quase
  idênticas.
- **O veto é do modelo, não de uma lista.** Manter lista de palavras proibidas
  por idioma × categoria é impossível. A mesma chamada que acha o termo do país
  devolve o que não contar. E a intenção de compra é lida do que os anunciantes
  pagam pela busca: ninguém dá lance em busca que não vende.

---

## 5. Observabilidade

Toda execução reporta cinco medidas, no log e como score no Langfuse:

| medida | definição |
|---|---|
| `task_completion_rate` | mercados pedidos que pontuaram ÷ mercados pedidos |
| `response_latency_seconds` | relógio de parede, do início até a resposta |
| `token_consumption` | somado do que o scoring reporta por header, mais o juiz |
| `tool_invocation_accuracy` | chamadas de coleta necessárias e no pedido ÷ todas |
| `hallucination_rate` | números do texto sem lastro na evidência ÷ números ditos |

Reportadas **também quando a execução falha**. Taxa de conclusão medida só nas
execuções que concluíram vale sempre 1,0, e latência medida só nelas esconde
justamente as mais demoradas.

**A verificação de alucinação tem duas metades.** A determinística confere todo
número do texto contra o pacote de evidência — exata, roda sempre, e só é
possível porque nenhum número vem do modelo (medido: *0 de 127 números sem
lastro*). O juiz é um segundo modelo, para as afirmações **sem número**. Sem
chave configurada, essa metade não roda e o log diz isso — ausência de
julgamento nunca é lida como aprovação.

Também vai ao log **o termo buscado em cada país**, porque é a única parte do
cálculo que um modelo escreveu e nada a jusante a revela: um termo nativo errado
produz uma análise perfeitamente formada das palavras erradas.

---

## 6. Frescor: quem sabe é quem tem

Cada serviço de coleta é dono do próprio frescor e responde em lote:
`GET /freshness?country=…&indicator=…&only_stale=true`. Diz **quando olhou pela
última vez**, não a data do dado — Banco Mundial e ONU publicam anualmente, e
uma regra escrita contra a data do dado recoletaria tudo todo dia para sempre.

Os três serviços macro têm CronJob diário. **PyTrends é a exceção por desenho:**
demanda é por produto e por palavra-chave, e não há como pré-armazenar todos os
produtos do mundo — é sempre sob demanda.

O scoring **não guarda mais cópia disso**. A tabela `coverage_index` existia
para decidir o que faltava e foi removida (migration 036): era um segundo
registro do mesmo fato, e os dois discordavam — um gap de demanda só nasce
porque o PyTrends acabou de dizer que não tem os termos, e o índice local ainda
podia chamá-lo de fresco e pular a coleta.

---

## 7. Decisões que parecem erradas e não são

| decisão | por quê |
|---|---|
| o banco continua `marketmind_core` | renomear banco vivo é outra operação, com outro risco, e nada lê significado do nome |
| `jobs`/`job_tasks` ficaram | não são frescor de ninguém: são o registro do que foi despachado, e é dele que sai a diferença entre "a fonte caiu" e "a fonte não tem" |
| a varredura de retomada roda dentro do scoring, não num CronJob | ela varre a tabela que esse serviço possui, e a primeira passada acontece no startup — que é exatamente quando existem órfãos, porque o processo acabou de morrer |
| `prewarm-macro` continua, mesmo com os CronJobs dos MS | os MS atualizam o que já têm; este garante o conjunto de países que o método precisa, que só ele conhece |
| liveness em `/healthz`, readiness em `/health` | reiniciar um processo não conserta um banco lento nem um vizinho lento. Reiniciar custa a análise em voo |
| o agrupamento de demanda roda numa thread | não fica paralelo (o GIL continua), fica **interrompível** — sem isso, nada era servido enquanto ele rodava, nem a probe |

---

## 8. O que ainda está aberto

| item | risco |
|---|---|
| **1 réplica de cada serviço** | qualquer reinício do agente mata a análise em voo; ela volta ~15 min depois pela varredura |
| **`JUDGE_SAMPLE_RATE=1.0` no dev** | correto aqui, caro em produção — um segundo modelo por execução dobra o custo do que está sendo medido |
| **Application do ArgoCD** | resolvida, mas o `app-of-apps` roda com `prune: false` de propósito: renomear uma Application exige criar a nova, sincronizar, e só então deletar a antiga |
| **volume de busca ≠ consumo** | limitação honesta do método: um produto básico comprado sem ser pesquisado é subestimado |

---

## 9. Ambiente

**Dev:** AKS, namespace `marketmind`, tudo em
`marketmind-dev.eastus.cloudapp.azure.com`.

| caminho | serviço |
|---|---|
| `/` | web |
| `/api` | **agente** — é por aqui que o navegador entra |
| `/orchestrator` | o mesmo agente, para exercitar direto |
| `/scoring` | o método, publicado só para inspeção |

ArgoCD sincroniza a partir de `equifina/beprofit-infra-manifests`; cada serviço
tem sua Application e o pipeline de cada repositório atualiza a tag da imagem no
values correspondente.
