"""Carrega os 127 países que foram MEDIDOS como tendo os dados, como candidatos.

2026-09-23. Entram com `analysis_ready = false`: estão no catálogo, ainda não
são oferecidos. Quem os promove é o pré-aquecimento, quando o dado deles
realmente chega (ver `MacroPrewarmService`). Oferecer antes de coletar seria
vender uma falha, porque a regra do grafo é que nada parcial é entregue: um país
pedido que não pontua faz a análise INTEIRA desistir, levando junto os países
bons escolhidos na mesma consulta.

COMO A LISTA FOI FEITA — e é reproduzível, não digitada:

  World Bank   os 12 indicadores, país por país, contra a API pública. Dos 217
               do catálogo, 159 têm todos. A Nigéria não tem 5.
  UN Data      os 4 indicadores, contra o portal de população. Os 135 que
               passaram no World Bank passaram todos aqui.
  DataForSEO   o país existir no catálogo de localizações (`location_type ==
               "Country"`, 213 entradas). Reprovou 8, e os 8 são Rússia,
               Belarus, Irã, Síria, Palestina, Kosovo, Hong Kong e Macau — o
               Google Ads não os atende. Essa coincidência é a melhor prova de
               que a medição mede alguma coisa.

A primeira medição estava errada e o erro vale guardar: tratava falha de rede
como ausência de dado, e 2604 requisições seguidas tomam limite de taxa. Dizia
que o Japão não tem população. Remedindo com repetição, e registrando o que não
se conseguiu ler como "não sei" em vez de "não tem", os 24 países que já
funcionam passaram todos — que é a verificação de sanidade que faltava.

AS COLUNAS, e por que ficam assim:

  undata_location_id  o mesmo código M49 numérico, que é a convenção das 24
                      linhas existentes e também o id do portal da ONU.
  oecd_ref_area       NULO, que é a blacklist: a OCDE só fornece o índice
                      imobiliário, e nem para todos. Argentina e Peru rodam
                      assim desde a migration 038. Um país nasce não-coberto
                      até que alguém meça — o lado seguro do erro.
  search_language     NULO de propósito. O idioma vem de quem escreve os termos
                      de busca, não de um cadastro (decisão de 22/09/2026).
  continent           região da ONU, com "Latin America and the Caribbean" e
                      "Northern America" virando "Americas", que é como as 24
                      linhas existentes escrevem.

Os nomes saem de uma REGRA, não de curadoria: o do World Bank, que é a
convenção das linhas atuais, trocado pelo da ONU quando o do World Bank vem
invertido com vírgula ("Egypt, Arab Rep.") ou perde acento ("Cote d'Ivoire").
"""

import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

# (iso3, nome, iso2, código M49, continente)
CANDIDATOS = [
    ("AFG", "Afghanistan", "AF", "004", "Asia"),
    ("AGO", "Angola", "AO", "024", "Africa"),
    ("ALB", "Albania", "AL", "008", "Europe"),
    ("ARM", "Armenia", "AM", "051", "Asia"),
    ("AUT", "Austria", "AT", "040", "Europe"),
    ("AZE", "Azerbaijan", "AZ", "031", "Asia"),
    ("BEL", "Belgium", "BE", "056", "Europe"),
    ("BEN", "Benin", "BJ", "204", "Africa"),
    ("BFA", "Burkina Faso", "BF", "854", "Africa"),
    ("BGD", "Bangladesh", "BD", "050", "Asia"),
    ("BGR", "Bulgaria", "BG", "100", "Europe"),
    ("BHR", "Bahrain", "BH", "048", "Asia"),
    ("BHS", "Bahamas", "BS", "044", "Americas"),
    ("BIH", "Bosnia and Herzegovina", "BA", "070", "Europe"),
    ("BLZ", "Belize", "BZ", "084", "Americas"),
    ("BOL", "Bolivia", "BO", "068", "Americas"),
    ("BRN", "Brunei Darussalam", "BN", "096", "Asia"),
    ("BTN", "Bhutan", "BT", "064", "Asia"),
    ("BWA", "Botswana", "BW", "072", "Africa"),
    ("CAF", "Central African Republic", "CF", "140", "Africa"),
    ("CHE", "Switzerland", "CH", "756", "Europe"),
    ("CHN", "China", "CN", "156", "Asia"),
    ("CIV", "Côte d'Ivoire", "CI", "384", "Africa"),
    ("CMR", "Cameroon", "CM", "120", "Africa"),
    ("COD", "Democratic Republic of the Congo", "CD", "180", "Africa"),
    ("COG", "Congo", "CG", "178", "Africa"),
    ("COM", "Comoros", "KM", "174", "Africa"),
    ("CRI", "Costa Rica", "CR", "188", "Americas"),
    ("CUW", "Curaçao", "CW", "531", "Americas"),
    ("CYP", "Cyprus", "CY", "196", "Asia"),
    ("CZE", "Czechia", "CZ", "203", "Europe"),
    ("DJI", "Djibouti", "DJ", "262", "Africa"),
    ("DNK", "Denmark", "DK", "208", "Europe"),
    ("DOM", "Dominican Republic", "DO", "214", "Americas"),
    ("DZA", "Algeria", "DZ", "012", "Africa"),
    ("ECU", "Ecuador", "EC", "218", "Americas"),
    ("EGY", "Egypt", "EG", "818", "Africa"),
    ("EST", "Estonia", "EE", "233", "Europe"),
    ("ETH", "Ethiopia", "ET", "231", "Africa"),
    ("FIN", "Finland", "FI", "246", "Europe"),
    ("FJI", "Fiji", "FJ", "242", "Oceania"),
    ("GAB", "Gabon", "GA", "266", "Africa"),
    ("GEO", "Georgia", "GE", "268", "Asia"),
    ("GHA", "Ghana", "GH", "288", "Africa"),
    ("GIN", "Guinea", "GN", "324", "Africa"),
    ("GMB", "Gambia", "GM", "270", "Africa"),
    ("GNB", "Guinea-Bissau", "GW", "624", "Africa"),
    ("GNQ", "Equatorial Guinea", "GQ", "226", "Africa"),
    ("GRC", "Greece", "GR", "300", "Europe"),
    ("GTM", "Guatemala", "GT", "320", "Americas"),
    ("HND", "Honduras", "HN", "340", "Americas"),
    ("HRV", "Croatia", "HR", "191", "Europe"),
    ("HTI", "Haiti", "HT", "332", "Americas"),
    ("HUN", "Hungary", "HU", "348", "Europe"),
    ("IRL", "Ireland", "IE", "372", "Europe"),
    ("IRQ", "Iraq", "IQ", "368", "Asia"),
    ("ISL", "Iceland", "IS", "352", "Europe"),
    ("ISR", "Israel", "IL", "376", "Asia"),
    ("KAZ", "Kazakhstan", "KZ", "398", "Asia"),
    ("KEN", "Kenya", "KE", "404", "Africa"),
    ("KGZ", "Kyrgyz Republic", "KG", "417", "Asia"),
    ("KHM", "Cambodia", "KH", "116", "Asia"),
    ("KIR", "Kiribati", "KI", "296", "Oceania"),
    ("KOR", "Republic of Korea", "KR", "410", "Asia"),
    ("KWT", "Kuwait", "KW", "414", "Asia"),
    ("LAO", "Lao PDR", "LA", "418", "Asia"),
    ("LBN", "Lebanon", "LB", "422", "Asia"),
    ("LBY", "Libya", "LY", "434", "Africa"),
    ("LKA", "Sri Lanka", "LK", "144", "Asia"),
    ("LSO", "Lesotho", "LS", "426", "Africa"),
    ("LTU", "Lithuania", "LT", "440", "Europe"),
    ("LUX", "Luxembourg", "LU", "442", "Europe"),
    ("LVA", "Latvia", "LV", "428", "Europe"),
    ("MAR", "Morocco", "MA", "504", "Africa"),
    ("MDA", "Moldova", "MD", "498", "Europe"),
    ("MDG", "Madagascar", "MG", "450", "Africa"),
    ("MDV", "Maldives", "MV", "462", "Asia"),
    ("MKD", "North Macedonia", "MK", "807", "Europe"),
    ("MLI", "Mali", "ML", "466", "Africa"),
    ("MLT", "Malta", "MT", "470", "Europe"),
    ("MNE", "Montenegro", "ME", "499", "Europe"),
    ("MNG", "Mongolia", "MN", "496", "Asia"),
    ("MRT", "Mauritania", "MR", "478", "Africa"),
    ("MUS", "Mauritius", "MU", "480", "Africa"),
    ("MYS", "Malaysia", "MY", "458", "Asia"),
    ("NAM", "Namibia", "NA", "516", "Africa"),
    ("NCL", "New Caledonia", "NC", "540", "Oceania"),
    ("NER", "Niger", "NE", "562", "Africa"),
    ("NIC", "Nicaragua", "NI", "558", "Americas"),
    ("NOR", "Norway", "NO", "578", "Europe"),
    ("NPL", "Nepal", "NP", "524", "Asia"),
    ("NZL", "New Zealand", "NZ", "554", "Oceania"),
    ("OMN", "Oman", "OM", "512", "Asia"),
    ("PAK", "Pakistan", "PK", "586", "Asia"),
    ("PAN", "Panama", "PA", "591", "Americas"),
    ("PHL", "Philippines", "PH", "608", "Asia"),
    ("PLW", "Palau", "PW", "585", "Oceania"),
    ("PRY", "Paraguay", "PY", "600", "Americas"),
    ("ROU", "Romania", "RO", "642", "Europe"),
    ("RWA", "Rwanda", "RW", "646", "Africa"),
    ("SDN", "Sudan", "SD", "729", "Africa"),
    ("SEN", "Senegal", "SN", "686", "Africa"),
    ("SGP", "Singapore", "SG", "702", "Asia"),
    ("SLB", "Solomon Islands", "SB", "090", "Oceania"),
    ("SLE", "Sierra Leone", "SL", "694", "Africa"),
    ("SLV", "El Salvador", "SV", "222", "Americas"),
    ("SMR", "San Marino", "SM", "674", "Europe"),
    ("SRB", "Serbia", "RS", "688", "Europe"),
    ("STP", "Sao Tome and Principe", "ST", "678", "Africa"),
    ("SVK", "Slovak Republic", "SK", "703", "Europe"),
    ("SVN", "Slovenia", "SI", "705", "Europe"),
    ("SWZ", "Eswatini", "SZ", "748", "Africa"),
    ("SYC", "Seychelles", "SC", "690", "Africa"),
    ("TCD", "Chad", "TD", "148", "Africa"),
    ("TGO", "Togo", "TG", "768", "Africa"),
    ("THA", "Thailand", "TH", "764", "Asia"),
    ("TJK", "Tajikistan", "TJ", "762", "Asia"),
    ("TLS", "Timor-Leste", "TL", "626", "Asia"),
    ("TON", "Tonga", "TO", "776", "Oceania"),
    ("TUN", "Tunisia", "TN", "788", "Africa"),
    ("TZA", "Tanzania", "TZ", "834", "Africa"),
    ("UGA", "Uganda", "UG", "800", "Africa"),
    ("UKR", "Ukraine", "UA", "804", "Europe"),
    ("UZB", "Uzbekistan", "UZ", "860", "Asia"),
    ("VNM", "Viet Nam", "VN", "704", "Asia"),
    ("ZMB", "Zambia", "ZM", "894", "Africa"),
    ("ZWE", "Zimbabwe", "ZW", "716", "Africa")
]


def upgrade() -> None:
    countries = sa.table(
        "countries",
        sa.column("iso3", sa.String),
        sa.column("name", sa.String),
        sa.column("iso2", sa.String),
        sa.column("un_numeric_code", sa.String),
        sa.column("undata_location_id", sa.String),
        sa.column("continent", sa.String),
        sa.column("analysis_ready", sa.Boolean),
    )
    # ON CONFLICT DO NOTHING: a migration tem de poder rodar num banco onde
    # algum destes países já exista, sem derrubar o deploy por causa disso.
    for iso3, nome, iso2, m49, continente in CANDIDATOS:
        op.execute(
            sa.text(
                # `created_at` é NOT NULL com default do lado do Python, que
                # SQL cru não aplica — sem isto a migration quebra o deploy.
                "INSERT INTO countries (iso3, name, iso2, un_numeric_code, undata_location_id, "
                "continent, analysis_ready, created_at) "
                "VALUES (:iso3, :name, :iso2, :m49, :m49, :cont, false, now() at time zone 'utc') "
                "ON CONFLICT (iso3) DO NOTHING"
            ).bindparams(iso3=iso3, name=nome, iso2=iso2, m49=m49, cont=continente)
        )


def downgrade() -> None:
    lista = ", ".join(f"'{c[0]}'" for c in CANDIDATOS)
    # Só os que continuam candidatos: um que já tenha sido promovido e analisado
    # não é removido por um downgrade de cadastro.
    op.execute(f"DELETE FROM countries WHERE iso3 IN ({lista}) AND analysis_ready = false")
