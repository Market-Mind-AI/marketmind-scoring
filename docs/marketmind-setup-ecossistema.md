# Cómo levantar el ecosistema MarketMind desde cero

Guía paso a paso para que otro desarrollador ejecute los 5 servicios en su propia máquina y procese la consulta de un
producto. Sigue exactamente este orden — los 4 microservicios de recolección deben estar arriba **antes** del Core.

## Prerrequisitos

- Docker + Docker Compose instalados.
- Los 5 repositorios clonados **en la misma carpeta padre** (ej. `~/Projects/marketmind-core`,
  `~/Projects/marketmind-pytrends`, `~/Projects/marketmind-worldbank`, `~/Projects/marketmind-oecd`,
  `~/Projects/marketmind-united-nations-data`) — no es obligatorio técnicamente, pero es lo que los `README.md` de
  cada uno asumen.

## Paso 1 — Levantar los 4 microservicios de recolección

Repite para cada uno de los 4 (`marketmind-pytrends`, `marketmind-worldbank`, `marketmind-oecd`,
`marketmind-united-nations-data`):

```bash
cd marketmind-<servicio>
cp .env.example .env          # ya viene con los puertos correctos para ejecutar los 5 juntos, no hace falta editar
docker compose up -d --build
docker compose exec marketmind-<servicio>-api alembic upgrade head
```

Al final, confirma que los 8 contenedores (4 API + 4 Postgres) están `Up`/`healthy`:

```bash
docker ps --filter "name=marketmind" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

Puertos esperados (host): PyTrends API `8000`/DB `5432`, OECD API `8001`/DB `5433`, UNData API `8002`/DB `5435`,
WorldBank API `8003`/DB `5434`.

## Paso 2 — Crear el usuario read-only en las 4 bases de datos

El Core lee estas 4 bases directo vía SQL (solo lectura). Ejecuta el script de abajo **una vez contra cada una** — es
idempotente, se puede volver a ejecutar sin problema:

```bash
cd marketmind-core
for combo in "5432 marketmind_pytrends" "5434 marketmind_worldbank" "5433 marketmind_oecd" "5435 marketmind_united_nations_data"; do
  port=$(echo $combo | cut -d' ' -f1); db=$(echo $combo | cut -d' ' -f2)
  PGPASSWORD=admin psql -h localhost -p $port -U marketmind -d $db -f scripts/create_readonly_roles.sql
done
```

(Necesita `psql` instalado en el host. Alternativa sin instalar nada: copia el archivo dentro de cada contenedor
Postgres con `docker cp` y ejecútalo con `docker exec`.)

## Paso 3 — Levantar el Core

```bash
cd marketmind-core
cp .env.example .env
docker compose up -d --build
docker compose exec marketmind-core-api alembic upgrade head
```

## Paso 4 — Confirmar que todo está conectado

```bash
curl -s http://localhost:8004/health | python3 -m json.tool
```

Se espera `"status": "ok"` y las 4 fuentes externas como `"connected"`. Si alguna aparece `"disconnected"`, lo más
probable es un puerto equivocado en el `.env` de algún servicio (revisar el Paso 1) o que el Paso 2 no se haya
ejecutado para esa fuente específicamente.

## Usuarios y contraseñas de las bases de datos

**Las 5 bases de datos usan la misma contraseña: `admin`.**

| Base de datos | Usuario admin | Usuario read-only (solo en los 4 microservicios) | Puerto (host) |
|---|---|---|---|
| marketmind_core | `marketmind` / `admin` | — | 5436 |
| marketmind_pytrends | `marketmind` / `admin` | `marketmind_readonly` / `admin` | 5432 |
| marketmind_oecd | `marketmind` / `admin` | `marketmind_readonly` / `admin` | 5433 |
| marketmind_worldbank | `marketmind` / `admin` | `marketmind_readonly` / `admin` | 5434 |
| marketmind_united_nations_data | `marketmind` / `admin` | `marketmind_readonly` / `admin` | 5435 |

## pgAdmin

Solo existe **una instancia**, levantada junto con el Core — no hay pgAdmin en los otros 4 servicios.

- URL: `http://localhost:5054`
- Login: `admin@marketmind.dev` / `admin`
- Las 5 bases de datos ya vienen registradas como servidores (`pgadmin/servers.json`), con la contraseña guardada —
  no debería pedir contraseña al hacer clic en ninguno de ellos. Si la pide, el volumen interno de pgAdmin necesita
  recrearse:
  ```bash
  cd marketmind-core
  docker compose down pgadmin
  docker volume rm marketmind-core_pgadmin_data
  docker compose up -d pgadmin
  ```

## Paso 5 — Procesar la consulta de un producto (flujo manual)

La Fase 5 (`POST /analyze`, un endpoint único) todavía no existe — hoy el flujo es manual, en 5 llamadas, en ese
orden. Ejemplo con "air fryer" en España:

```bash
BASE=http://localhost:8004

# 1) Resuelve/crea el producto en el catálogo
curl -s -X POST $BASE/catalog/resolve -H "Content-Type: application/json" \
  -d '{"text": "air fryer", "source": "manual_test"}'
# → devuelve un "entity_key" (UUID) — usar en los próximos pasos

# 2) Calcula los indicadores del Capítulo 4 para el país + producto
curl -s -X POST $BASE/indicators/compute -H "Content-Type: application/json" \
  -d '{"country": "ESP", "entity_key": "<entity_key del paso 1>"}'

# 3) Recalcula los límites de normalización (0-100) contra la población de referencia
# ⚠️ removido: la ruta /admin/indicators/refresh-reference ya no existe (verificado 25/08/2026);
# los bounds vienen sembrados por migrations. Ver transparencia-tecnica §2.

# 4) Recalcula los 5 pesos de dimensión (ROC — instantáneo, no lee ningún dato)
curl -s -X POST "$BASE/admin/mos/refresh-weights"

# 5) Calcula el MOS final
curl -s -X POST $BASE/mos/compute -H "Content-Type: application/json" \
  -d '{"entity_key": "<entity_key del paso 1>", "countries": ["ESP"]}'
```

**El orden importa:** volver a ejecutar el paso 2 después del paso 3/4 es necesario si quieres ver `normalized_score`
completado — la primera vez que se calcula un indicador, si la referencia todavía no existe para el período,
`normalized_score` queda en `null` (con una nota explicando el motivo), aunque el `raw_value` ya esté correcto.

Documentación interactiva de todas las rutas: `http://localhost:8004/docs` (Swagger, generado automáticamente por
FastAPI).

## Dónde revisar cada resultado en la base de datos

Conectándote a la base de datos `marketmind_core` (vía pgAdmin o `psql`):

| Qué quieres ver | Tabla | Columna principal |
|---|---|---|
| Métrica normalizada (indicador del Cap. 4, 0-100) | `indicator_values` | `normalized_score` (`raw_value` tiene el valor bruto antes de normalizar) |
| Peso de cada dimensión (Cap. 5, ROC) | `indicator_weights` | `weight` (una fila por dimensión + `period_key`) |
| MOS final | `mos_results` | `mos` (`mos_before_cap` muestra el valor antes del techo anticompensación; `capped_by` indica si el techo fue aplicado) |

Ejemplo de consulta para ver todo de un producto/país junto:

```sql
-- Métricas normalizadas del producto + país por país, para el período actual
SELECT indicator_code, entity_key, country, raw_value, normalized_score, availability_status
FROM indicator_values
WHERE country = 'ESP' AND (entity_key = '<entity_key>' OR entity_key IS NULL)
ORDER BY indicator_code;

-- Pesos de las 5 dimensiones usados en el período
SELECT dimension, weight FROM indicator_weights ORDER BY weight DESC;

-- MOS final
SELECT country, mos, mos_before_cap, capped_by, topsis_score, availability_status, excluded_reason
FROM mos_results
WHERE entity_key = '<entity_key>';
```

Si `mos` viene `NULL`, el motivo siempre está en `excluded_reason` (nunca queda sin explicación) — el caso más común es
que falte un peso calibrado para el `period_key` (ejecutar el paso 4) o que un indicador obligatorio no tenga dato
(`availability_status` distinto de `available` en `indicator_values` para ese país).
