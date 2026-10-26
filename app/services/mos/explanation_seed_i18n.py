"""Versões em português ("pt") e espanhol ("es") do conteúdo-semente de explanation_seed.py:
rótulos, significados (os textos de interpretation.py) e frases por faixa de cada métrica.
Uma migration grava estas linhas no banco ao lado das inglesas; as linhas em inglês continuam
sendo as canônicas, e estas são só a tradução delas. As chaves (códigos de métrica e rótulos
de faixa em inglês) são idênticas às da fonte; só os valores mudam."""

LABELS: dict[str, dict[str, str]] = {
    "pt": {
        # --- indicators ---
        "trend_score": "Volume de Demanda",
        "growth_score": "Crescimento da Demanda",
        "demand_stability_score": "Estabilidade da Demanda",
        "regional_demand_score": "Distribuição Regional da Demanda",
        "search_intent_score": "Intenção de Compra",
        "gdp_per_capita_score": "PIB per Capita",
        "internet_penetration_score": "Penetração da Internet",
        "mobile_connectivity_score": "Conectividade Móvel",
        "broadband_adoption_score": "Adoção de Banda Larga",
        "gdp_growth_score": "Crescimento do PIB",
        "population_scale_score": "Escala Populacional",
        "urbanization_score": "Urbanização",
        "worldbank_inflation_score": "Inflação",
        "population_growth_score": "Crescimento Populacional",
        "net_migration_rate_score": "Migração Líquida",
        "median_age_fit_score": "Adequação da Idade Mediana",
        "consumer_spending_score": "Gastos do Consumidor",
        "government_expenditure_score": "Gastos do Governo",
        "category_volume_score": "Demanda da Categoria",
        "investment_score": "Investimento",
        "net_exports_score": "Exportações Líquidas",
        "trade_openness_score": "Abertura Comercial",
        "real_estate_growth_score": "Crescimento Imobiliário",
        # --- dimensions ---
        "demand_volume": "Volume de Demanda",
        "demand_quality": "Qualidade da Demanda",
        "macro_demand_structure": "Estrutura Macro da Demanda",
        "market_size": "Tamanho do Mercado",
        "economic_readiness": "Prontidão Econômica",
        "digital_readiness": "Prontidão Digital",
        "demographic_sustainability": "Sustentabilidade Demográfica",
        # --- mos ---
        "mos": "Pontuação de Oportunidade de Mercado",
    },
    "es": {
        # --- indicators ---
        "trend_score": "Volumen de Demanda",
        "growth_score": "Crecimiento de la Demanda",
        "demand_stability_score": "Estabilidad de la Demanda",
        "regional_demand_score": "Distribución Regional de la Demanda",
        "search_intent_score": "Intención de Compra",
        "gdp_per_capita_score": "PIB per Cápita",
        "internet_penetration_score": "Penetración de Internet",
        "mobile_connectivity_score": "Conectividad Móvil",
        "broadband_adoption_score": "Adopción de Banda Ancha",
        "gdp_growth_score": "Crecimiento del PIB",
        "population_scale_score": "Escala de Población",
        "urbanization_score": "Urbanización",
        "worldbank_inflation_score": "Inflación",
        "population_growth_score": "Crecimiento de la Población",
        "net_migration_rate_score": "Migración Neta",
        "median_age_fit_score": "Ajuste de la Edad Mediana",
        "consumer_spending_score": "Gasto del Consumidor",
        "government_expenditure_score": "Gasto Público",
        "category_volume_score": "Demanda de la Categoría",
        "investment_score": "Inversión",
        "net_exports_score": "Exportaciones Netas",
        "trade_openness_score": "Apertura Comercial",
        "real_estate_growth_score": "Crecimiento Inmobiliario",
        # --- dimensions ---
        "demand_volume": "Volumen de Demanda",
        "demand_quality": "Calidad de la Demanda",
        "macro_demand_structure": "Estructura Macro de la Demanda",
        "market_size": "Tamaño del Mercado",
        "economic_readiness": "Preparación Económica",
        "digital_readiness": "Preparación Digital",
        "demographic_sustainability": "Sostenibilidad Demográfica",
        # --- mos ---
        "mos": "Puntuación de Oportunidad de Mercado",
    },
}

MEANINGS: dict[str, dict[str, str]] = {
    "pt": {
        # --- indicators ---
        "trend_score": (
            "VOLUME DE DEMANDA — o total absoluto de buscas online mensais para comprar este produto neste país "
            "(o raw_value é essa contagem real). É o POTENCIAL de mercado / espaço para vendas, normalizado "
            "contra o MERCADO DE REFERÊNCIA DO PRODUTO (o país de maior demanda) como teto de 100 pontos: a "
            "pontuação é a demanda deste país como percentual do maior mercado nacional do produto, de modo que "
            "um produto de nicho é medido na sua própria escala e não numa escala global fixa, e 2x as buscas "
            "~ 2x a pontuação. IMPORTANTE para a narrativa: um volume grande pode vir de (a) simples tamanho da "
            "população ou (b) apetite genuíno (uma parcela alta da população querendo o produto) — leia isto "
            "junto com o valor de 'penetration' nas notas (buscas por 100 mil habitantes) para distinguir os "
            "dois. Volume alto E penetração alta = mercado GRANDE e entusiasmado; volume alto mas penetração "
            "baixa = grande só por ser populoso; volume baixo mas penetração alta = pequeno, mas com forte "
            "apetite (potencial real por pessoa)."
        ),
        "growth_score": "A demanda por este produto está crescendo ou caindo?",
        "demand_stability_score": "Quão estável (ou volátil) é a demanda ao longo do tempo?",
        "regional_demand_score": "O interesse está espalhado pelo país ou concentrado em poucas regiões?",
        "search_intent_score": "As buscas relacionadas mostram intenção de compra ou apenas interesse geral?",
        "gdp_per_capita_score": "Quanto poder de compra médio este mercado tem?",
        "internet_penetration_score": "Que parcela da população consegue descobrir/comprar o produto online?",
        "mobile_connectivity_score": (
            "Quão difundidas estão as linhas móveis? Assinaturas móveis por 100 pessoas, saturando em uma "
            "linha por pessoa: acima disso o número conta chips (pré-pagos, dual-SIM, M2M), não alcance, "
            "então todo mercado com uma linha por pessoa ou mais aparece como plenamente atendido."
        ),
        "broadband_adoption_score": (
            "Quanta infraestrutura de banda larga fixa está instalada? Assinaturas por 100 pessoas — uma "
            "linha atende um domicílio, então isto mede qualidade de acesso e não quantidade de dispositivos."
        ),
        "gdp_growth_score": "A economia está se expandindo ou se contraindo?",
        "population_scale_score": "Qual o tamanho da população endereçável?",
        "urbanization_score": (
            "Que parcela da população vive em áreas urbanas (logística e alcance do varejo mais fáceis)?"
        ),
        "worldbank_inflation_score": "Quão alta está a inflação recente dos preços ao consumidor?",
        "population_growth_score": "A população está crescendo ou diminuindo?",
        "net_migration_rate_score": "O país está ganhando ou perdendo população por migração?",
        "median_age_fit_score": "Quão próxima está a idade mediana da população da faixa etária-alvo deste produto?",
        "consumer_spending_score": "Quanto da economia é movido pelo consumo das famílias?",
        "government_expenditure_score": (
            "Quanto da economia é movido pelo gasto do governo — apenas contexto, não pontuado: parcelas "
            "muito altas (ex.: os Estados de bem-estar nórdicos) e muito baixas aparecem tanto em mercados "
            "consumidores fortes quanto em fracos, então não há uma direção defensável de oportunidade de "
            "mercado."
        ),
        "category_volume_score": (
            "Quanta demanda de busca online existe aqui para a CATEGORIA inteira do produto — apenas contexto, "
            "não pontuado: é a escala contra a qual a demanda da própria marca deve ser lida. Uma marca com "
            "pouca demanda num mercado onde a categoria é muito buscada está perdendo para concorrentes, não "
            "diante de um mercado inexistente."
        ),
        "investment_score": (
            "Quanto da economia está sendo reinvestido em capital fixo "
            "(um indicador indireto de infraestrutura e investimento produtivo)?"
        ),
        "net_exports_score": "O país é exportador líquido ou importador líquido de bens e serviços?",
        "trade_openness_score": (
            "Quão integrada ao comércio global está esta economia (exportações + importações como parcela do "
            "PIB)? Maior abertura em geral significa um caminho regulatório/logístico mais fácil para um "
            "produto estrangeiro entrar."
        ),
        "real_estate_growth_score": (
            "O mercado imobiliário está aquecendo ou esfriando? (taxa de crescimento dos preços reais de "
            "imóveis — o nível de preço em si não é comparável entre países, já que o índice da OECD por "
            "trás é rebaseado em 100 para cada país)"
        ),
        # --- dimensions ---
        "demand_volume": (
            "QUANTAS pessoas de fato querem comprar este produto aqui — o tamanho absoluto da demanda real de "
            "compra online (total de buscas mensais). É o sinal de potencial de vendas/tamanho de mercado e a "
            "dimensão de maior prioridade: um mercado com muito mais compradores interessados deve ficar acima "
            "de um com melhores condições econômicas mas pouca demanda. Cruze os números absolutos por país e "
            "a penetração (buscas por 100 mil habitantes) para explicar SE o volume vem do tamanho da população "
            "ou de apetite genuíno."
        ),
        "demand_quality": (
            "Quão BOA é essa demanda, independentemente do tamanho do mercado — ela está crescendo, é estável "
            "ao longo do tempo e orientada à compra (pessoas buscando para comprar) em vez de mera curiosidade? "
            "Um mercado pequeno pode pontuar muito alto aqui: qualidade forte sinaliza potencial real mesmo "
            "quando o volume absoluto é modesto."
        ),
        "macro_demand_structure": (
            "A composição da demanda agregada do país (PIB = Consumo + Investimento + Gastos do governo + "
            "Exportações líquidas) — quanto da economia é movido por consumo privado, investimento e "
            "integração comercial."
        ),
        "market_size": "A escala do público endereçável neste país.",
        "economic_readiness": "O poder de compra do mercado, seu dinamismo econômico e a estabilidade de preços.",
        "digital_readiness": "Se a população consegue descobrir e comprar o produto online.",
        "demographic_sustainability": (
            'Tendências populacionais de longo prazo — menos ligadas a "isto vende agora" do que as outras dimensões.'
        ),
        # --- mos ---
        "mos": (
            "PONTUAÇÃO DE OPORTUNIDADE DE MERCADO — o veredito único de 0 a 100 sobre quão atraente este país é "
            "para vender este produto agora, agregando todas as dimensões (volume e qualidade da demanda, "
            "estrutura macro da demanda, prontidão econômica e digital, tamanho do mercado, sustentabilidade "
            "demográfica) com pesos de prioridade Rank-Reciprocal via TOPSIS. Mais alto = um lugar melhor para "
            "vender. É um sinal de RANKING ('onde vender primeiro'), não uma previsão de vendas: leia junto "
            "com o detalhamento por dimensão e sinal para explicar POR QUE um país fica onde fica."
        ),
    },
    "es": {
        # --- indicators ---
        "trend_score": (
            "VOLUMEN DE DEMANDA — el total absoluto de búsquedas online mensuales para comprar este producto en "
            "este país (el raw_value es ese recuento real). Es el POTENCIAL de mercado / margen de ventas, "
            "normalizado frente al MERCADO DE REFERENCIA DEL PRODUCTO (su país de mayor demanda) como techo de "
            "100 puntos: la puntuación es la demanda de este país como porcentaje del mayor mercado nacional "
            "del producto, de modo que un producto de nicho se mide en su propia escala y no en una escala "
            "global fija, y 2x las búsquedas ~ 2x la puntuación. IMPORTANTE para la narrativa: un volumen "
            "grande puede venir de (a) el simple tamaño de la población o (b) un apetito genuino (una "
            "proporción alta de la población que lo quiere) — léalo junto con el valor de 'penetration' en "
            "las notas (búsquedas por cada 100 mil habitantes) para distinguirlos. Volumen alto Y penetración "
            "alta = mercado GRANDE y entusiasta; volumen alto pero penetración baja = grande solo por ser "
            "populoso; volumen bajo pero penetración alta = pequeño pero con fuerte apetito (potencial real "
            "por persona)."
        ),
        "growth_score": "¿La demanda de este producto está creciendo o disminuyendo?",
        "demand_stability_score": "¿Qué tan estable (o volátil) es la demanda a lo largo del tiempo?",
        "regional_demand_score": "¿El interés está repartido por el país o concentrado en pocas regiones?",
        "search_intent_score": "¿Las búsquedas relacionadas muestran intención de compra o solo interés general?",
        "gdp_per_capita_score": "¿Cuánto poder adquisitivo medio tiene este mercado?",
        "internet_penetration_score": "¿Qué proporción de la población puede descubrir/comprar el producto online?",
        "mobile_connectivity_score": (
            "¿Qué tan extendidas están las líneas móviles? Suscripciones móviles por cada 100 personas, con "
            "saturación en una línea por persona: por encima de eso el número cuenta tarjetas SIM (prepago, "
            "dual-SIM, M2M), no alcance, así que todo mercado con una línea por persona o más figura como "
            "plenamente atendido."
        ),
        "broadband_adoption_score": (
            "¿Cuánta infraestructura de banda ancha fija está instalada? Suscripciones por cada 100 personas — "
            "una línea da servicio a un hogar, así que esto mide la calidad del acceso y no el número de "
            "dispositivos."
        ),
        "gdp_growth_score": "¿La economía se está expandiendo o contrayendo?",
        "population_scale_score": "¿Qué tamaño tiene la población a la que se puede llegar?",
        "urbanization_score": (
            "¿Qué proporción de la población vive en zonas urbanas (logística y alcance minorista más fáciles)?"
        ),
        "worldbank_inflation_score": "¿Qué tan alta es la inflación reciente de los precios al consumidor?",
        "population_growth_score": "¿La población está creciendo o disminuyendo?",
        "net_migration_rate_score": "¿El país está ganando o perdiendo población por migración?",
        "median_age_fit_score": (
            "¿Qué tan cerca está la edad mediana de la población del rango de edad objetivo de este producto?"
        ),
        "consumer_spending_score": "¿Qué parte de la economía está impulsada por el consumo de los hogares?",
        "government_expenditure_score": (
            "¿Qué parte de la economía está impulsada por el gasto público — solo contexto, no puntuado: "
            "tanto proporciones muy altas (p. ej., los Estados de bienestar nórdicos) como muy bajas se dan "
            "por igual en mercados de consumo fuertes y débiles, así que no hay una dirección defendible de "
            "oportunidad de mercado."
        ),
        "category_volume_score": (
            "¿Cuánta demanda de búsqueda online existe aquí para toda la CATEGORÍA del producto — solo "
            "contexto, no puntuado: es la escala frente a la cual debe leerse la demanda propia de esta marca. "
            "Una marca con poca demanda en un mercado donde la categoría se busca mucho está perdiendo frente "
            "a la competencia, no ante un mercado inexistente."
        ),
        "investment_score": (
            "¿Qué parte de la economía se está reinvirtiendo en capital fijo "
            "(un indicador indirecto de infraestructura e inversión productiva)?"
        ),
        "net_exports_score": "¿El país es exportador neto o importador neto de bienes y servicios?",
        "trade_openness_score": (
            "¿Qué tan integrada está esta economía en el comercio global (exportaciones + importaciones como "
            "proporción del PIB)? Una mayor apertura suele significar un camino regulatorio/logístico más "
            "fácil para que entre un producto extranjero."
        ),
        "real_estate_growth_score": (
            "¿El mercado inmobiliario se está calentando o enfriando? (tasa de crecimiento de los precios "
            "reales de la vivienda — el nivel de precios en sí no es comparable entre países, ya que el índice "
            "de la OECD subyacente se rebasa a 100 en cada país)"
        ),
        # --- dimensions ---
        "demand_volume": (
            "CUÁNTAS personas quieren realmente comprar este producto aquí — el tamaño absoluto de la demanda "
            "real de compra online (total de búsquedas mensuales). Es la señal de potencial de ventas/tamaño "
            "de mercado y la dimensión de mayor prioridad: un mercado con muchos más compradores interesados "
            "debe quedar por encima de uno con mejores condiciones económicas pero poca demanda. Cruce las "
            "cifras absolutas por país y la penetración (búsquedas por cada 100 mil habitantes) para explicar "
            "SI el volumen proviene del tamaño de la población o de un apetito genuino."
        ),
        "demand_quality": (
            "Qué tan BUENA es esa demanda, independientemente del tamaño del mercado — ¿está creciendo, es "
            "estable en el tiempo y está orientada a la compra (gente que busca para comprar) en lugar de mera "
            "curiosidad? Un mercado pequeño puede puntuar muy alto aquí: una calidad fuerte señala potencial "
            "real incluso cuando el volumen absoluto es modesto."
        ),
        "macro_demand_structure": (
            "La composición de la demanda agregada del país (PIB = Consumo + Inversión + Gasto público + "
            "Exportaciones netas) — qué parte de la economía está impulsada por el consumo privado, la "
            "inversión y la integración comercial."
        ),
        "market_size": "La escala de la audiencia alcanzable en este país.",
        "economic_readiness": "El poder adquisitivo del mercado, su impulso económico y la estabilidad de precios.",
        "digital_readiness": "Si la población puede descubrir y comprar el producto online.",
        "demographic_sustainability": (
            'Tendencias de población a largo plazo — menos ligadas a "¿se venderá ahora?" que las demás dimensiones.'
        ),
        # --- mos ---
        "mos": (
            "PUNTUACIÓN DE OPORTUNIDAD DE MERCADO — el veredicto único de 0 a 100 sobre qué tan atractivo es "
            "este país para vender este producto ahora mismo, agregando todas las dimensiones (volumen y "
            "calidad de la demanda, estructura macro de la demanda, preparación económica y digital, tamaño "
            "del mercado, sostenibilidad demográfica) con pesos de prioridad Rank-Reciprocal mediante TOPSIS. "
            "Más alto = un mejor lugar para vender. Es una señal de RANKING ('dónde vender primero'), no una "
            "previsión de ventas: léala junto con el desglose por dimensión y señal para explicar POR QUÉ un "
            "país queda donde queda."
        ),
    },
}

BAND_PHRASES: dict[str, dict[str, dict[str, str]]] = {
    "pt": {
        # --- indicators ---
        "trend_score": {
            "Very Low": "Quase ninguém está buscando comprar este produto aqui",
            "Low": "Poucos compradores ativos - a demanda de compra é escassa",
            "Moderate": "Uma base modesta, mas real, de pessoas querendo comprar",
            "High": "Forte demanda de compra - muitos compradores ativos neste mercado",
            "Very High": "Demanda de compra enorme - um número imenso de compradores ativos",
        },
        "growth_score": {
            "Very Low": "A demanda está caindo forte - o interesse está secando",
            "Low": "A demanda está recuando - um mercado em leve declínio",
            "Moderate": "A demanda está praticamente estável ao longo do tempo",
            "High": "A demanda está subindo - um mercado em crescimento e expansão",
            "Very High": "A demanda está disparando - o interesse cresce rápido",
        },
        "demand_stability_score": {
            "Very Low": "A demanda oscila muito - altamente errática e imprevisível",
            "Low": "A demanda é volátil e difícil de planejar",
            "Moderate": "A demanda é razoavelmente estável, com oscilações ocasionais",
            "High": "A demanda é estável e confiável mês a mês",
            "Very High": "A demanda é muito estável - consistente e fácil de prever",
        },
        "regional_demand_score": {
            "Very Low": "O interesse está concentrado em pouquíssimas regiões",
            "Low": "O interesse se agrupa em um punhado de regiões líderes",
            "Moderate": "O interesse está moderadamente distribuído entre as regiões",
            "High": "O interesse alcança a maioria das regiões do país",
            "Very High": "O interesse está distribuído de forma uniforme por todo o país",
        },
        "search_intent_score": {
            "Very Low": "As buscas mostram sobretudo curiosidade, não intenção de comprar",
            "Low": "Sinais de compra fracos - aqui pesquisar pesa mais que comprar",
            "Moderate": "Uma mistura equilibrada de compradores e curiosos",
            "High": "As buscas sinalizam clara intenção de comprar o produto",
            "Very High": "As buscas mostram intenção de compra forte e imediata",
        },
        "gdp_per_capita_score": {
            "Very Low": "Poder de compra médio muito limitado neste mercado",
            "Low": "O baixo poder de compra limita quanto as pessoas podem gastar",
            "Moderate": "Poder de compra moderado, adequado a preços de mercado de massa",
            "High": "Forte poder de compra - os clientes podem pagar por produtos premium",
            "Very High": "Poder de compra muito alto - um mercado abastado e de alto consumo",
        },
        "internet_penetration_score": {
            "Very Low": "A maioria das pessoas não consegue chegar ao produto online",
            "Low": "Alcance online limitado - muita gente ainda está offline",
            "Moderate": "Cerca de metade da população pode comprar online",
            "High": "A maioria das pessoas pode descobrir e comprar online",
            "Very High": "Quase todos estão online e alcançáveis digitalmente",
        },
        "mobile_connectivity_score": {
            "Very Low": "Pouquíssimas pessoas têm linha móvel própria",
            "Low": "As linhas móveis alcançam só parte da população",
            "Moderate": "Cerca de metade da população tem linha móvel própria",
            "High": "A maioria das pessoas tem linha móvel própria",
            "Very High": "Pelo menos uma linha móvel por pessoa - o alcance móvel está saturado",
        },
        "broadband_adoption_score": {
            "Very Low": "Quase nenhuma infraestrutura de banda larga fixa instalada",
            "Low": "A banda larga fixa chega a uma pequena parcela dos domicílios",
            "Moderate": "A banda larga fixa chega a uma parcela razoável dos domicílios",
            "High": "A banda larga fixa está amplamente instalada nos domicílios",
            "Very High": "Cobertura densa de banda larga fixa - forte infraestrutura de acesso",
        },
        "gdp_growth_score": {
            "Very Low": "A economia está em forte contração neste momento",
            "Low": "A economia está estagnada ou encolhendo levemente",
            "Moderate": "A economia está crescendo em ritmo modesto",
            "High": "A economia está se expandindo com vigor",
            "Very High": "A economia está em forte alta - expansão econômica acelerada",
        },
        "population_scale_score": {
            "Very Low": "Uma população endereçável muito pequena no geral",
            "Low": "Uma população pequena limita o público potencial",
            "Moderate": "Uma população de porte médio a ser atendida",
            "High": "Uma população grande oferece amplo alcance",
            "Very High": "Uma população vasta - um público endereçável enorme",
        },
        "urbanization_score": {
            "Very Low": "Uma população majoritariamente rural - logística e alcance difíceis",
            "Low": "Predominantemente rural - alcance de varejo e entrega limitado",
            "Moderate": "Uma combinação equilibrada de população urbana e rural",
            "High": "Predominantemente urbana - logística e alcance de varejo fáceis",
            "Very High": "Altamente urbanizada - muito fácil de alcançar e atender",
        },
        "worldbank_inflation_score": {
            "Very Low": "Uma inflação descontrolada está corroendo fortemente o poder de compra das famílias",
            "Low": "A inflação alta está apertando o quanto as famílias podem gastar",
            "Moderate": "Inflação moderada, com alguma pressão sobre os preços",
            "High": "A inflação é baixa - os preços estão em grande parte sob controle",
            "Very High": "Preços muito estáveis, poder de compra bem preservado",
        },
        "population_growth_score": {
            "Very Low": "A população está encolhendo rápido - uma base em contração",
            "Low": "A população está diminuindo lentamente",
            "Moderate": "A população está praticamente estável",
            "High": "A população está crescendo de forma constante",
            "Very High": "A população está crescendo rápido - uma base em expansão",
        },
        "net_migration_rate_score": {
            "Very Low": "O país está perdendo muita gente para a emigração",
            "Low": "Uma saída líquida de pessoas por migração",
            "Moderate": "Entradas e saídas migratórias do país estão equilibradas",
            "High": "O país está atraindo novos residentes no saldo",
            "Very High": "Uma forte entrada de migrantes está ampliando o mercado",
        },
        "median_age_fit_score": {
            "Very Low": "A idade típica está longe dos compradores-alvo do produto",
            "Low": "O perfil etário se ajusta mal ao público-alvo",
            "Moderate": "O perfil etário corresponde em parte aos compradores-alvo",
            "High": "O perfil etário se ajusta bem ao público-alvo",
            "Very High": "A idade típica corresponde quase perfeitamente aos compradores-alvo",
        },
        "consumer_spending_score": {
            "Very Low": "As famílias movem muito pouco desta economia",
            "Low": "O consumo das famílias tem papel econômico limitado",
            "Moderate": "O consumo das famílias é uma parcela moderada da economia",
            "High": "O consumo das famílias impulsiona boa parte da economia",
            "Very High": "Uma economia movida pelo consumo - as famílias gastam muito",
        },
        "government_expenditure_score": {
            "Very Low": "A economia é movida principalmente pela atividade privada",
            "Low": "O governo tem um papel pequeno na economia",
            "Moderate": "Gastos públicos e privados estão razoavelmente equilibrados",
            "High": "O governo tem grande presença na economia",
            "Very High": "A economia depende fortemente do gasto público",
        },
        "category_volume_score": {
            "Very Low": "A categoria em si quase não é buscada aqui",
            "Low": "A categoria atrai pouca demanda de busca aqui",
            "Moderate": "A categoria tem demanda de busca moderada aqui",
            "High": "A categoria é muito buscada aqui",
            "Very High": "A categoria é um dos grandes temas de busca deste mercado",
        },
        "investment_score": {
            "Very Low": "Muito pouco está sendo investido em capacidade produtiva",
            "Low": "Baixo investimento em infraestrutura e capital",
            "Moderate": "Um nível moderado de investimento de capital",
            "High": "Forte investimento em infraestrutura e capacidade produtiva",
            "Very High": "Investimento pesado alimentando a capacidade produtiva futura",
        },
        "net_exports_score": {
            "Very Low": "Um grande importador líquido com amplo déficit comercial",
            "Low": "As importações superam as exportações - um déficit comercial",
            "Moderate": "Exportações e importações estão aproximadamente equilibradas",
            "High": "Um exportador líquido com superávit comercial",
            "Very High": "Um forte exportador líquido com amplo superávit comercial",
        },
        "trade_openness_score": {
            "Very Low": "Uma economia bastante fechada - difícil para produtos estrangeiros entrarem",
            "Low": "A abertura comercial limitada complica a entrada no mercado",
            "Moderate": "Moderadamente aberta ao comércio internacional",
            "High": "Aberta ao comércio - um caminho tranquilo para produtos estrangeiros",
            "Very High": "Uma economia muito aberta - muito fácil para importações entrarem",
        },
        "real_estate_growth_score": {
            "Very Low": "Os preços dos imóveis estão caindo forte - um mercado esfriando",
            "Low": "Os preços dos imóveis estão perdendo força no mercado",
            "Moderate": "Os preços dos imóveis estão praticamente estáveis",
            "High": "Os preços dos imóveis estão subindo - um mercado aquecendo",
            "Very High": "Os preços dos imóveis estão disparando - um mercado em forte alta",
        },
        # --- dimensions ---
        "demand_volume": {
            "Very Low": "Quase não existe demanda real de compra aqui",
            "Low": "Um grupo reduzido de compradores efetivos neste mercado",
            "Moderate": "Uma base sólida, de porte médio, de compradores reais",
            "High": "Uma grande base de pessoas querendo ativamente comprar",
            "Very High": "Um enorme grupo de compradores reais - tamanho de mercado de primeira linha",
        },
        "demand_quality": {
            "Very Low": "A demanda é fraca, errática e raramente voltada à compra",
            "Low": "A qualidade da demanda é baixa - instável e mais de pesquisa que de compra",
            "Moderate": "A qualidade da demanda é razoável, mas um tanto irregular",
            "High": "Demanda de alta qualidade - crescente, estável e voltada à compra",
            "Very High": "Demanda excelente - crescendo forte, estável e pronta para comprar",
        },
        "macro_demand_structure": {
            "Very Low": "A macroeconomia sustenta mal a demanda do consumidor",
            "Low": "Fundamentos macro fracos para a demanda do consumidor",
            "Moderate": "Condições macro moderadamente favoráveis à demanda",
            "High": "A estrutura macro sustenta fortemente a demanda do consumidor",
            "Very High": "Uma composição macro muito favorável à demanda do consumidor",
        },
        "market_size": {
            "Very Low": "Um mercado endereçável muito pequeno no geral",
            "Low": "Um público endereçável limitado para trabalhar",
            "Moderate": "Um mercado endereçável de porte médio a ser atingido",
            "High": "Um grande público endereçável a ser alcançado",
            "Very High": "Um mercado endereçável vasto e de amplo alcance",
        },
        "economic_readiness": {
            "Very Low": "Poder de compra fraco e preços instáveis",
            "Low": "Poder de gasto limitado e base econômica frágil",
            "Moderate": "Poder de compra e estabilidade econômica moderados",
            "High": "Forte poder de compra e uma economia estável",
            "Very High": "Uma economia abastada e estável, plenamente pronta para gastar",
        },
        "digital_readiness": {
            "Very Low": "Pouquíssimos conseguem descobrir ou comprar o produto online",
            "Low": "Alcance digital limitado para vender online",
            "Moderate": "Acesso digital moderado para o comércio online",
            "High": "Forte alcance digital - a maioria pode comprar online",
            "Very High": "Um mercado totalmente digital - quase todos podem comprar online",
        },
        "demographic_sustainability": {
            "Very Low": "A demografia de longo prazo aponta para um mercado encolhendo",
            "Low": "As tendências demográficas são desfavoráveis no longo prazo",
            "Moderate": "A demografia é, em geral, estável no longo prazo",
            "High": "Tendências demográficas favoráveis de longo prazo sustentam o mercado",
            "Very High": "Impulso demográfico de longo prazo muito forte",
        },
        # --- mos ---
        "mos": {
            "Very Low Opportunity": "Evitar por enquanto - um lugar ruim para vender este produto",
            "Low Opportunity": "Um mercado fraco - deixar atrás de opções melhores",
            "Moderate Opportunity": "Um mercado mediano - viável, mas não prioritário",
            "Good Opportunity": "Um mercado sólido que vale a pena buscar",
            "High Opportunity": "Um mercado forte - priorizar para entrada antecipada",
            "Excellent Opportunity": "Um mercado de máxima prioridade para entrar primeiro",
        },
    },
    "es": {
        # --- indicators ---
        "trend_score": {
            "Very Low": "Casi nadie está buscando comprar este producto aquí",
            "Low": "Pocos compradores activos - la demanda de compra es escasa",
            "Moderate": "Una base modesta pero real de personas que quieren comprar",
            "High": "Fuerte demanda de compra - muchos compradores activos en este mercado",
            "Very High": "Demanda de compra enorme - una cantidad inmensa de compradores activos",
        },
        "growth_score": {
            "Very Low": "La demanda está cayendo con fuerza - el interés se está agotando",
            "Low": "La demanda está retrocediendo - un mercado en leve declive",
            "Moderate": "La demanda se mantiene prácticamente estable en el tiempo",
            "High": "La demanda está subiendo - un mercado en crecimiento y expansión",
            "Very High": "La demanda se está disparando - el interés crece rápido",
        },
        "demand_stability_score": {
            "Very Low": "La demanda oscila muchísimo - muy errática e impredecible",
            "Low": "La demanda es volátil y difícil de planificar",
            "Moderate": "La demanda es bastante estable, con oscilaciones ocasionales",
            "High": "La demanda es estable y fiable mes a mes",
            "Very High": "La demanda es muy estable - constante y fácil de prever",
        },
        "regional_demand_score": {
            "Very Low": "El interés está concentrado en muy pocas regiones",
            "Low": "El interés se agrupa en un puñado de regiones líderes",
            "Moderate": "El interés está moderadamente repartido entre las regiones",
            "High": "El interés llega a la mayoría de las regiones del país",
            "Very High": "El interés está repartido de forma uniforme por todo el país",
        },
        "search_intent_score": {
            "Very Low": "Las búsquedas muestran sobre todo curiosidad, no intención de compra",
            "Low": "Señales de compra débiles - aquí pesa más curiosear que comprar",
            "Moderate": "Una mezcla equilibrada de compradores y curiosos",
            "High": "Las búsquedas señalan una clara intención de comprar el producto",
            "Very High": "Las búsquedas muestran una intención de compra fuerte e inmediata",
        },
        "gdp_per_capita_score": {
            "Very Low": "Poder adquisitivo medio muy limitado en este mercado",
            "Low": "El bajo poder adquisitivo limita cuánto puede gastar la gente",
            "Moderate": "Poder adquisitivo moderado, acorde con precios de gama media",
            "High": "Fuerte poder adquisitivo - los clientes pueden permitirse productos premium",
            "Very High": "Poder adquisitivo muy alto - un mercado acomodado y de alto gasto",
        },
        "internet_penetration_score": {
            "Very Low": "La mayoría de la gente no puede llegar al producto online",
            "Low": "Alcance online limitado - mucha gente sigue desconectada",
            "Moderate": "Alrededor de la mitad de la población puede comprar online",
            "High": "La mayoría de la gente puede descubrir y comprar online",
            "Very High": "Casi todos están online y son alcanzables digitalmente",
        },
        "mobile_connectivity_score": {
            "Very Low": "Muy pocas personas tienen una línea móvil propia",
            "Low": "Las líneas móviles llegan solo a parte de la población",
            "Moderate": "Alrededor de la mitad de la población tiene línea móvil propia",
            "High": "La mayoría de la gente tiene línea móvil propia",
            "Very High": "Al menos una línea móvil por persona - el alcance móvil está saturado",
        },
        "broadband_adoption_score": {
            "Very Low": "Casi no hay infraestructura de banda ancha fija instalada",
            "Low": "La banda ancha fija llega a una pequeña parte de los hogares",
            "Moderate": "La banda ancha fija llega a una parte razonable de los hogares",
            "High": "La banda ancha fija está ampliamente instalada en los hogares",
            "Very High": "Cobertura densa de banda ancha fija - sólida infraestructura de acceso",
        },
        "gdp_growth_score": {
            "Very Low": "La economía se está contrayendo con fuerza en este momento",
            "Low": "La economía está estancada o se reduce ligeramente",
            "Moderate": "La economía crece a un ritmo modesto",
            "High": "La economía se está expandiendo con vigor",
            "Very High": "La economía está en auge - expansión económica acelerada",
        },
        "population_scale_score": {
            "Very Low": "Una población alcanzable muy pequeña en conjunto",
            "Low": "Una población pequeña limita la audiencia potencial",
            "Moderate": "Una población de tamaño medio a la que dirigirse",
            "High": "Una población grande ofrece un amplio alcance",
            "Very High": "Una población enorme - una audiencia alcanzable inmensa",
        },
        "urbanization_score": {
            "Very Low": "Una población mayoritariamente rural - logística y alcance difíciles",
            "Low": "Predominantemente rural - alcance minorista y de entrega limitado",
            "Moderate": "Una mezcla equilibrada de población urbana y rural",
            "High": "Predominantemente urbana - logística y alcance minorista sencillos",
            "Very High": "Muy urbanizada - muy fácil de alcanzar y atender",
        },
        "worldbank_inflation_score": {
            "Very Low": "Una inflación desbocada está erosionando con fuerza el poder adquisitivo de los hogares",
            "Low": "La inflación alta está recortando lo que los hogares pueden gastar",
            "Moderate": "Inflación moderada, con cierta presión sobre los precios",
            "High": "La inflación es baja - los precios están en gran medida bajo control",
            "Very High": "Precios muy estables, poder adquisitivo bien preservado",
        },
        "population_growth_score": {
            "Very Low": "La población se está reduciendo rápido - una base en contracción",
            "Low": "La población está disminuyendo lentamente",
            "Moderate": "La población se mantiene prácticamente estable",
            "High": "La población crece de forma sostenida",
            "Very High": "La población crece rápido - una base en expansión",
        },
        "net_migration_rate_score": {
            "Very Low": "El país está perdiendo mucha gente por la emigración",
            "Low": "Una salida neta de personas por migración",
            "Moderate": "Las entradas y salidas migratorias del país están equilibradas",
            "High": "El país atrae nuevos residentes en términos netos",
            "Very High": "Una fuerte entrada de migrantes está ampliando el mercado",
        },
        "median_age_fit_score": {
            "Very Low": "La edad típica está lejos de los compradores objetivo del producto",
            "Low": "El perfil de edad encaja mal con el público objetivo",
            "Moderate": "El perfil de edad coincide en parte con los compradores objetivo",
            "High": "El perfil de edad encaja bien con el público objetivo",
            "Very High": "La edad típica coincide casi a la perfección con los compradores objetivo",
        },
        "consumer_spending_score": {
            "Very Low": "Los hogares impulsan muy poco de esta economía",
            "Low": "El gasto de consumo tiene un papel económico limitado",
            "Moderate": "El gasto de consumo es una parte moderada de la economía",
            "High": "El consumo de los hogares impulsa buena parte de la economía",
            "Very High": "Una economía impulsada por el consumo - los hogares gastan mucho",
        },
        "government_expenditure_score": {
            "Very Low": "La economía está impulsada sobre todo por la actividad privada",
            "Low": "El gobierno tiene un papel pequeño en la economía",
            "Moderate": "El gasto público y el privado están bastante equilibrados",
            "High": "El gobierno tiene una gran presencia en la economía",
            "Very High": "La economía depende en gran medida del gasto público",
        },
        "category_volume_score": {
            "Very Low": "La categoría en sí apenas se busca aquí",
            "Low": "La categoría atrae poca demanda de búsqueda aquí",
            "Moderate": "La categoría tiene una demanda de búsqueda moderada aquí",
            "High": "La categoría se busca mucho aquí",
            "Very High": "La categoría es uno de los grandes temas de búsqueda de este mercado",
        },
        "investment_score": {
            "Very Low": "Se invierte muy poco en capacidad productiva",
            "Low": "Baja inversión en infraestructura y capital",
            "Moderate": "Un nivel moderado de inversión de capital",
            "High": "Fuerte inversión en infraestructura y capacidad productiva",
            "Very High": "Una inversión elevada que impulsa la capacidad productiva futura",
        },
        "net_exports_score": {
            "Very Low": "Un gran importador neto con un amplio déficit comercial",
            "Low": "Las importaciones superan a las exportaciones - un déficit comercial",
            "Moderate": "Exportaciones e importaciones están aproximadamente equilibradas",
            "High": "Un exportador neto con superávit comercial",
            "Very High": "Un fuerte exportador neto con un amplio superávit comercial",
        },
        "trade_openness_score": {
            "Very Low": "Una economía bastante cerrada - difícil para que entren productos extranjeros",
            "Low": "La apertura comercial limitada complica la entrada al mercado",
            "Moderate": "Moderadamente abierta al comercio internacional",
            "High": "Abierta al comercio - un camino fluido para los productos extranjeros",
            "Very High": "Una economía muy abierta - muy fácil para que entren las importaciones",
        },
        "real_estate_growth_score": {
            "Very Low": "Los precios de la vivienda caen con fuerza - un mercado que se enfría",
            "Low": "Los precios de la vivienda se están debilitando en el mercado",
            "Moderate": "Los precios de la vivienda están prácticamente estables",
            "High": "Los precios de la vivienda suben - un mercado que se calienta",
            "Very High": "Los precios de la vivienda se disparan - un mercado en auge",
        },
        # --- dimensions ---
        "demand_volume": {
            "Very Low": "Apenas existe demanda real de compra aquí",
            "Low": "Un grupo reducido de compradores reales en este mercado",
            "Moderate": "Una base sólida y de tamaño medio de compradores reales",
            "High": "Una gran base de personas que quieren comprar activamente",
            "Very High": "Un enorme grupo de compradores reales - tamaño de mercado de primer nivel",
        },
        "demand_quality": {
            "Very Low": "La demanda es débil, errática y rara vez orientada a la compra",
            "Low": "La calidad de la demanda es pobre - inestable y más de curioseo que de compra",
            "Moderate": "La calidad de la demanda es aceptable pero algo irregular",
            "High": "Demanda de alta calidad - creciente, estable y orientada a la compra",
            "Very High": "Demanda excelente - en fuerte crecimiento, estable y lista para comprar",
        },
        "macro_demand_structure": {
            "Very Low": "La macroeconomía apenas respalda la demanda de consumo",
            "Low": "Fundamentos macro débiles para la demanda de consumo",
            "Moderate": "Condiciones macro moderadamente favorables para la demanda",
            "High": "La estructura macro respalda con fuerza la demanda de consumo",
            "Very High": "Una composición macro muy favorable para la demanda de consumo",
        },
        "market_size": {
            "Very Low": "Un mercado alcanzable muy pequeño en conjunto",
            "Low": "Una audiencia alcanzable limitada con la que trabajar",
            "Moderate": "Un mercado alcanzable de tamaño medio al que dirigirse",
            "High": "Una gran audiencia alcanzable a la que llegar",
            "Very High": "Un mercado alcanzable enorme y de amplio alcance",
        },
        "economic_readiness": {
            "Very Low": "Poder adquisitivo débil y precios inestables",
            "Low": "Capacidad de gasto limitada y una base económica frágil",
            "Moderate": "Poder adquisitivo y estabilidad económica moderados",
            "High": "Fuerte poder adquisitivo y una economía estable",
            "Very High": "Una economía acomodada y estable, plenamente lista para gastar",
        },
        "digital_readiness": {
            "Very Low": "Muy pocos pueden descubrir o comprar el producto online",
            "Low": "Alcance digital limitado para vender online",
            "Moderate": "Acceso digital moderado para el comercio online",
            "High": "Fuerte alcance digital - la mayoría puede comprar online",
            "Very High": "Un mercado plenamente digital - casi todos pueden comprar online",
        },
        "demographic_sustainability": {
            "Very Low": "La demografía a largo plazo apunta a un mercado que se reduce",
            "Low": "Las tendencias demográficas son desfavorables a largo plazo",
            "Moderate": "La demografía es en general estable a largo plazo",
            "High": "Tendencias demográficas favorables a largo plazo respaldan el mercado",
            "Very High": "Un impulso demográfico a largo plazo muy fuerte",
        },
        # --- mos ---
        "mos": {
            "Very Low Opportunity": "Evitar por ahora - un mal lugar para vender este producto",
            "Low Opportunity": "Un mercado débil - dejarlo detrás de mejores opciones",
            "Moderate Opportunity": "Un mercado intermedio - viable pero no prioritario",
            "Good Opportunity": "Un mercado sólido que vale la pena abordar",
            "High Opportunity": "Un mercado fuerte - priorizarlo para una entrada temprana",
            "Excellent Opportunity": "Un mercado de máxima prioridad para entrar primero",
        },
    },
}
