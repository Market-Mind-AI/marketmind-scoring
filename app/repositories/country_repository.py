from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.country import Country


class CountryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_iso3(self, iso3: str) -> Country | None:
        result = await self._session.execute(select(Country).where(Country.iso3 == iso3))
        return result.scalar_one_or_none()

    async def list_including_candidates(self) -> list[Country]:
        """Todos os países da tabela, prontos e candidatos.

        Só para quem precisa AQUECER o dado: um candidato não pode ser medido
        sem que suas séries tenham sido coletadas alguma vez. Nunca para
        oferecer, nunca para calibrar — ver `list_all`.
        """
        result = await self._session.execute(
            select(Country).order_by(Country.continent, Country.name)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[Country]:
        """A cesta de países DISPONÍVEIS para análise: os que já foram medidos e
        pontuam. Ordenada por continente e nome, para o front agrupar e listar
        direto nessa ordem. É também a população fixa de referência da
        calibragem de pesos.

        Desde 23/09/2026 a tabela guarda mais que isso: candidatos entram como
        `analysis_ready = false` e só passam a ser oferecidos depois de medidos
        (ver a migration 042). Este método continua devolvendo só os prontos, e
        DE PROPÓSITO — é o que preserva o significado de tudo que já o chamava.

        Em especial a calibragem: os pesos do Cap. 5 saem da população de
        referência, então incluir candidatos aqui mudaria os pesos e, com eles,
        TODO MOS já calculado. Seria uma mudança de método entrando como efeito
        colateral de um cadastro.

        Quem precisa dos candidatos — hoje só o pré-aquecimento, que é como eles
        conseguem dado para serem medidos — usa `list_including_candidates`.
        """
        result = await self._session.execute(
            select(Country).where(Country.analysis_ready.is_(True)).order_by(Country.continent, Country.name)
        )
        return list(result.scalars().all())
