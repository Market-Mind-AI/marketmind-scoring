# Deploy do MarketMind na Azure da BeProfit (GitOps cross-org)

Plano de integração do MarketMind (org GitHub `Market-Mind-AI`) à infraestrutura
Azure/AKS da BeProfit (org GitHub `equifina`, repo `beprofit-infra-manifests`).

Contexto: projeto de mestrado (TFM) patrocinado pela BeProfit. Roda na conta Azure
deles, mas **não** sob o domínio `beprofit.ai` — usa o domínio padrão da Azure
(`*.cloudapp.azure.com`).

## Decisões tomadas (2026-08-07)

| Decisão | Escolha | Motivo |
| --- | --- | --- |
| Onde vivem os manifests | Dentro de `equifina/beprofit-infra-manifests` | ArgoCD não precisa de credencial nova; uma única visão da infra Azure |
| Banco | Azure PostgreSQL Flexible Server B1ms/B2s, 1 servidor / 5 databases | `vector` + `pg_trgm` disponíveis; backup gerenciado; zero mudança no código do Core |
| Escopo da 1ª rodada | Core + AI web + 4 coletores (6 serviços) | Fluxo completo do MOS. `marketmind-ollma-llm` fica de fora (precisaria de node pool com mais RAM) |
| Domínio | `marketmind-dev.<região>.cloudapp.azure.com` | Ingress controller e Public IP próprios, isolados do `beprofit.ai` |
| Cluster | `aks-beprofit-dev`, namespace `marketmind` | Reusa o cluster dev existente, isolado por namespace + AppProject |

## O padrão BeProfit que estamos copiando

Por serviço:

1. Workflow no repo do serviço → build **arm64** → `ghcr.io/<org>/<svc>:sha-<12>`
2. Mesmo workflow faz checkout do `beprofit-infra-manifests` com `INFRA_REPO_TOKEN`
   e faz patch do `tag:` em `env/<env>/<svc>-values.yaml`
3. ArgoCD (`selfHeal: true`) detecta o commit e faz o rollout

Manifests: `charts/<svc>/` (Helm) + `env/<env>/<svc>-values.yaml` +
`argocd/apps/<env>/<svc>-<env>.yaml`, registrado no `kustomization.yaml` do env.

Referência viva: `beprofit-quota-management` é o serviço mais parecido com o Core
(API + banco + migrations) — vale usar o `build-and-push.yml` e o
`charts/beprofit-quota/` dele como template.

## 1. Fronteira cross-org GitHub

### GHCR

Imagens publicadas em `ghcr.io/market-mind-ai/*` (owner natural do repo, nenhum
token extra pra escrever — `secrets.GITHUB_TOKEN` basta pro push).

O AKS precisa de um pull secret **novo** na namespace `marketmind`:
`ghcr-marketmind-pull-secret`, com PAT `read:packages` de um machine user do
Market-Mind-AI. Se as imagens forem públicas, esse secret desaparece.

### Bump de tag no infra-manifests

`secrets.GITHUB_TOKEN` **não cruza org**. Solução: org-level secret
`INFRA_REPO_TOKEN` no `Market-Mind-AI` = PAT fine-grained com `Contents: write`
restrito a `equifina/beprofit-infra-manifests`.

(Alternativa mais limpa e mais trabalhosa: GitHub App instalado nos dois orgs,
mintando installation token de 1h no workflow.)

### ArgoCD

**Nada a fazer.** Como os manifests ficam no repo que o Argo já lê, não há
credencial nem `sourceRepos` novo. (O AppProject já usa `sourceRepos: '*'`.)

### ⚠️ arm64 obrigatório

Todo node do AKS é Ampere arm64 (`Standard_D4pls_v5` / `D2ps_v5`). Imagem amd64
**não agenda**. Nos workflows: `runs-on: ubuntu-24.04-arm` e
`platforms: linux/arm64`. Nunca emular via QEMU (o comentário no workflow do quota
documenta um build Angular de ~20 min cancelado por causa disso).

Nenhum Dockerfile do MarketMind impede arm64 (`python:3.12-slim` é multi-arch;
wheels aarch64 existem pra scipy/pandas/matplotlib do `pymcdm`).

## 2. Domínio: `cloudapp.azure.com`

O domínio padrão da Azure é `<label>.<região>.cloudapp.azure.com`, e o label é
propriedade do **Public IP**, não de um hostname arbitrário. Um IP = um hostname.

### Ingress controller próprio

Novo `Application` de ingress-nginx, IngressClass e Public IP dedicados:

```yaml
controller:
  ingressClassResource:
    name: nginx-marketmind
    enabled: true
    default: false
  ingressClass: nginx-marketmind
  service:
    type: LoadBalancer
    externalTrafficPolicy: Local
    annotations:
      service.beta.kubernetes.io/azure-dns-label-name: marketmind-dev
```

→ `marketmind-dev.<região>.cloudapp.azure.com`

Custo: um LB rule + um IP público (poucos USD/mês). O que compra: um `host:`
errado num Ingress do MarketMind não tem como aterrissar no controller do
`beprofit.ai`, porque a IngressClass é outra.

**Por que não reusar o controller da BeProfit:** anotar aquele Service é
exatamente onde eles já se queimaram — `argocd/apps/shared/ingress-nginx.yaml`
carrega `ignoreDifferences` em anotações dele por causa de drift com o ArgoCD.

### TLS

Não dá pra usar o ClusterIssuer `letsencrypt-azure-dns` existente: o solver DNS-01
dele tem credencial só da zona `beprofit.ai` no Azure DNS.

Precisa de ClusterIssuer novo com **HTTP-01**:

```yaml
solvers:
  - http01:
      ingress:
        class: nginx-marketmind
```

`*.cloudapp.azure.com` está na Public Suffix List, então cada label conta como
domínio registrado próprio pro rate limit da Let's Encrypt.

⚠️ **Único passo do plano não verificado previamente** — confirmar a emissão do
certificado na primeira tentativa.

### external-dns

**Nada a fazer.** O `domainFilters` dele é `beprofit.ai`, e o registro A do
`cloudapp.azure.com` é publicado pela própria Azure a partir do label do IP.
(O `policy: upsert-only` de lá também já é proteção contra deleção acidental.)

### Roteamento: um hostname, paths

Só vai pra ingress o que precisa ser público:

| Path | Serviço | Exposição |
| --- | --- | --- |
| `/` | `marketmind-ai` (web) | Ingress |
| `/api` | `marketmind-core` | Ingress |
| — | `marketmind-pytrends` | ClusterIP |
| — | `marketmind-worldbank` | ClusterIP |
| — | `marketmind-oecd` | ClusterIP |
| — | `marketmind-united-nations-data` | ClusterIP |

Mesma decisão do `beprofit-quota` ("No ingress, deliberately"). Os coletores ficam
alcançáveis em `http://marketmind-pytrends.marketmind.svc.cluster.local:8000` —
o que **elimina o hack de `host.docker.internal`** do `docker-compose.yml` e a
tabela de portas distintas por serviço do README (cada Service tem seu próprio DNS,
todos podem usar 8000).

### ⚠️ Anotações do Ingress do Core

**Correção (2026-08-07):** a versão anterior deste doc dizia que `POST /analyze`
responde por SSE. Isso vinha do plano original, não do código. Verificado em
`app/api/routes/analyze.py` e em `marketmind-ai/web/src/lib/api.ts`: o Core
**não tem nenhum endpoint SSE** — `POST /analyze` devolve 202 com um `request_id`
e o frontend faz polling em `GET /analyze/{request_id}`. Não há stream pra
bufferizar, então `proxy-buffering: off` não é necessário.

O que o Ingress do Core realmente precisa é outra coisa:

```yaml
# POST /catalog/identify-image manda a foto em base64 no corpo JSON. O default
# do ingress-nginx é 1m, que uma foto de celular estoura fácil — e o 413 vem do
# nginx, nunca chega na aplicação, então parece bug de frontend.
nginx.ingress.kubernetes.io/proxy-body-size: "10m"

# identify-image é síncrono e faz uma chamada de visão + uma de enriquecimento
# antes de responder. Esse teto é pra esses round-trips de LLM, não pra análise
# (que retorna 202 na hora).
nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
nginx.ingress.kubernetes.io/proxy-send-timeout: "300"

# O Core monta os routers na raiz (/analyze, /catalog, /health), então o prefixo
# /api precisa ser removido antes de chegar nele.
nginx.ingress.kubernetes.io/rewrite-target: /$2
```

## 3. Isolamento no cluster

- **AppProject novo** `marketmind` — não adicionar namespaces ao AppProject
  `beprofit`. Mantém RBAC e blast radius separados, e traduz corretamente a relação
  "patrocinado por", não "parte do produto".
  - ⚠️ O comentário no `argocd/projects/beprofit.yaml` documenta uma falha silenciosa:
    Application cujo namespace de destino não está listado no AppProject fica em
    `Unknown/Unknown` com `InvalidSpecError` e **nunca tenta sincronizar** — parece
    sync travado, não sync rejeitado. Listar `marketmind` explicitamente.
- **Namespace** `marketmind` no cluster `aks-beprofit-dev`.
- **App-of-apps** próprio: `argocd/app-of-apps-marketmind.yaml` +
  `argocd/apps/marketmind/kustomization.yaml`.
- **Node pool:** tolerar o taint `workload: apps` existente pra reusar o pool
  (custo zero). Pool dedicado exigiria VM nova.

```yaml
nodeSelector:
  workload: apps
tolerations:
  - key: workload
    operator: Equal
    value: apps
    effect: NoSchedule
```

## 4. Estado

### Postgres

Um **Azure PostgreSQL Flexible Server** (Burstable B1ms ou B2s) com 5 databases:
`marketmind_core`, `marketmind_pytrends`, `marketmind_worldbank`,
`marketmind_oecd`, `marketmind_undata`.

Parâmetro de servidor: `azure.extensions = VECTOR,PG_TRGM`.

Um servidor, 5 connection strings — que é exatamente como o Core já se conecta
hoje (engines separadas por fonte). A role `marketmind_readonly` e o
`scripts/create_readonly_roles.sql` continuam valendo **sem mudança de código**.

⚠️ **Migrations:** o `Dockerfile` do Core roda `alembic upgrade head` no `CMD`, o
que corre com múltiplas réplicas. Manter `replicaCount: 1` no Core. Se precisar
escalar depois, copiar o padrão de PreSync Job do
`charts/beprofit-quota/templates/migrations-job.yaml` (imagem de migrations
separada, mesma tag da imagem da API — o workflow do quota faz o bump das duas
juntas justamente pra evitar skew).

**pgAdmin não vai pro cluster.**

### Secrets

O ESO (External Secrets Operator) já está instalado no cluster dev.

- Key Vault novo: `kv-marketmind-dev`
- `SecretStore` namespaced em `marketmind` (não ClusterSecretStore — isolamento)
- `ExternalSecret` → Secret `marketmind-core-env`, consumido via `envFromSecrets`
- Stakater Reloader (`reloader.enabled: true`) reinicia o pod na rotação

Menor caminho de setup: dar role `Key Vault Secrets User` ao UAMI `id-eso-dev`
que já existe, sobre o vault novo.

Secrets necessários: `OPENAI_API_KEY` (embeddings, `text-embedding-3-small`) e as
senhas dos 5 databases.

## 5. Ordem de execução

1. Workflows de build nos 6 repos do Market-Mind-AI (arm64 → GHCR do próprio org)
2. Org secret `INFRA_REPO_TOKEN` no Market-Mind-AI
3. Azure: PostgreSQL Flexible Server + Key Vault + machine user/PAT do GHCR
4. `charts/marketmind-*` + `env/marketmind-dev/*-values.yaml` no infra-manifests
5. AppProject + app-of-apps + ingress-nginx secundário + ClusterIssuer HTTP-01
6. Adicionar o step de bump de tag nos workflows do passo 1

## Verificações já feitas

- `scripts/sync-image-tags.sh` (cron de 10 min no infra-manifests) tem lista de
  serviços **hardcoded** e org `equifina/` fixo. Não vai tocar nos arquivos do
  MarketMind. Sem colisão. (O `git add env/*/*-values.yaml` do workflow pegaria os
  arquivos novos no glob, mas o script nunca os modifica.)
- O AppProject `beprofit` usa `sourceRepos: '*'` — se um dia os manifests migrarem
  pro org acadêmico, não há restrição de repo a mudar, só a credencial do Argo.

## Implementação (2026-08-07)

Tudo abaixo já está escrito. Runbook operacional do lado da BeProfit:
`beprofit-infra-manifests/docs/marketmind-cross-org-gitops.md`.

### Nos 6 repos do Market-Mind-AI

- `.github/workflows/build-and-push.yml` nos 6 (arm64, GHCR do próprio org, bump
  de tag no infra-manifests via `INFRA_REPO_TOKEN`).
- `marketmind-ai`: `Dockerfile` (Astro static → nginx-unprivileged), `.dockerignore`,
  `nginx/default.conf`. **Imagem construída e testada localmente** — arm64,
  `/report/{id}` e `/r/{slug}` servem o placeholder com o URL preservado (rewrite
  interno, não redirect), `/naoexiste` devolve 404 de verdade, e `PUBLIC_API_BASE`
  compila para `/api` no bundle.
- Nos 5 serviços Python: `app/db/ssl_config.py` + `DATABASE_SSL_MODE`
  (`disable`/`require`/`verify-full`, default `disable`). Senha agora
  percent-encoded na DSN — validado com `@`, espaço e `/` e o roundtrip do
  SQLAlchemy. **O UNData é o único síncrono** (psycopg2), então recebe `?sslmode=`
  na URL; os outros 4 recebem um `SSLContext` via `connect_args`, porque asyncpg
  não entende `sslmode`.

### No infra-manifests

- `charts/marketmind-app/` — um chart pros 6 workloads (os 4 coletores são
  idênticos; 6 cópias iriam divergir). `nameOverride` é obrigatório, com `fail`
  no helper — sem isso os 6 colidiriam num Deployment só.
- `env/marketmind-dev/` — 6 values + `platform/` (ClusterIssuer HTTP-01 +
  Certificate) + `secrets/` (ClusterSecretStore + 5 ExternalSecret + pull secret).
- `argocd/projects/marketmind.yaml` (whitelist explícita, não `'*'`),
  `argocd/app-of-apps-marketmind.yaml`, `argocd/apps/marketmind/` (9 Applications).
- `scripts/prepare-marketmind-azure.sh` — Postgres + 5 databases +
  `azure.extensions=VECTOR,PG_TRGM` + firewall pro egress do AKS + Key Vault +
  roles + seed.

Validado: `helm template` nos 6 values, `kubectl kustomize` nas Applications,
parse YAML dos 31 arquivos, e o regex de bump de tag contra um values de amostra
(só o bloco `image:` de topo muda; um `tag:` em `podAnnotations` fica intacto).

### Achados durante a implementação

- **O Core não tem SSE.** Ver a correção na seção 2 — era suposição do plano.
- **`proxy-body-size`**: o default de 1MB do ingress-nginx quebraria
  `POST /catalog/identify-image` (foto em base64). Corrigido pra 10m.
- **`controllerValue` do segundo ingress-nginx**: se ficasse no default, o
  controller do MarketMind serviria *também* todos os Ingress do `beprofit.ai`.
- **Dois PATs do GitHub em texto puro** no repo da BeProfit — migrados pro Key
  Vault. Ver seção abaixo.
- **`.env.example` do Core está desatualizado**: aponta os 4 sites de LLM pra
  `ollama`, mas o default do `config.py` é `gpt`. Os values na Azure setam `gpt`
  explicitamente, então tirar o Ollama do escopo não quebra nada.

## Pendências / não verificado

- Emissão de certificado Let's Encrypt via HTTP-01 em `cloudapp.azure.com`
  (esperado funcionar, confirmar na prática). **Único passo do plano que não
  consegui validar antes.**
- Disponibilidade do label `marketmind-dev` em `eastus.cloudapp.azure.com` — é
  único por região, e só dá pra saber quando o Public IP for provisionado.
- Nada foi commitado nem enviado pra nenhum repo. Todas as mudanças estão só no
  working tree local dos 7 repositórios.
- `marketmind-ollma-llm` fora do escopo: Ollama em node arm64 sem GPU
  (`D4pls_v5`, 4 vCPU / 8 GB) roda modelo pequeno em CPU e devagar. Exigiria node
  pool próprio com mais RAM.
