# Goat Tips — Módulo de Retreinamento do Modelo

Job de retreinamento do modelo Poisson de previsão de partidas da Premier League. Executado como um **IBM Code Engine Job** em agendamento diário.

---

## Visão Geral

Este módulo é responsável por:

1. Buscar partidas encerradas do **Supabase** (PostgreSQL)
2. Enriquecer os índices de força de cada equipe com estatísticas de jogadores do **FBref via Kaggle**
3. Treinar o modelo **Poisson independente** (inspirado em Dixon-Coles)
4. Persistir o artefato serializado (`.pkl`) e o cartão do modelo (`.json`) no **IBM Cloud Object Storage**
5. Materializar snapshots de features nas tabelas do Supabase para uso pelo agente `/ask`

---

## Arquitetura

```
Supabase (PostgreSQL)          Kaggle / FBref CSV
        │                              │
        ▼                              ▼
  load_training_data()    load_kaggle_player_features()
        │                              │
        └──────────┬───────────────────┘
                   ▼
              train()
          (Modelo Poisson)
                   │
        ┌──────────┴─────────────────┐
        ▼                            ▼
  IBM COS (poisson_model.pkl)   Supabase Snapshots
  IBM COS (model_card.json)     ├── team_player_strength_snapshot
                                ├── team_style_snapshot_statsbomb
                                └── player_absence_impact
```

---

## Modelo

**Algoritmo:** Modelo de Poisson Independente com ajuste por xG e enriquecimento por índices de jogadores FBref.

**Versão:** `2.1.0`

**Features por equipe:**

| Feature | Fonte | Descrição |
|---|---|---|
| `attack` / `defense` | Supabase | Força geral de ataque/defesa normalizada pela média da liga |
| `attack_home` / `attack_away` | Supabase | Força de ataque separada por mandante/visitante |
| `xg_attack_home` / `xg_defense_home` | Supabase (`match_stats`) | Ajuste baseado em xG (mínimo 10 partidas) |
| `attack_index` | Kaggle FBref | Índice ponderado de gols + xG por 90 min |
| `creation_index` | Kaggle FBref | Assistências + xAG + KP + progressões por 90 min |
| `defensive_index` | Kaggle FBref | Desarmes + Interceptações + Bloqueios por 90 min |
| `squad_depth` | Kaggle FBref | Número de jogadores com ≥ 5 partidas de 90 min |

---

## Estrutura do Repositório

```
goat-tips-azr-app-retrain-model/
├── retrain.py          # Lógica completa de retreinamento
├── Dockerfile          # Imagem Python 3.12-slim para IBM Code Engine
├── requirements.txt    # Dependências Python
├── deploy.sh           # Script de build e deploy no IBM Code Engine
├── .env                # Variáveis de ambiente (não versionado)
└── data/
    └── kaggle/
        ├── players_data_2025_2026.csv        # Estatísticas FBref (Kaggle)
        └── statsbomb_premier_league_matches.csv  # Partidas StatsBomb
```

---

## Variáveis de Ambiente

| Variável | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| `SUPABASE_DB_URL` | ✅ | — | Connection string PostgreSQL do Supabase |
| `IBM_COS_ACCESS_KEY_ID` | ✅ | — | Chave HMAC de acesso ao IBM COS |
| `IBM_COS_SECRET_ACCESS_KEY` | ✅ | — | Chave HMAC secreta do IBM COS |
| `IBM_COS_ENDPOINT` | ❌ | `https://s3.us-south.cloud-object-storage.appdomain.cloud` | Endpoint do IBM COS |
| `IBM_COS_BUCKET` | ❌ | `goat-tips-bucket` | Nome do bucket COS |
| `MODEL_BLOB_NAME` | ❌ | `poisson_model.pkl` | Chave do objeto do modelo |
| `MODEL_CARD_BLOB_NAME` | ❌ | `model_card.json` | Chave do objeto do cartão do modelo |
| `KAGGLE_PLAYERS_CSV` | ❌ | `data/kaggle/players_data_2025_2026.csv` | Caminho para o CSV de jogadores FBref |
| `STATSBOMB_MATCHES_CSV` | ❌ | `data/kaggle/statsbomb_premier_league_matches.csv` | Caminho para o CSV de partidas StatsBomb |
| `SNAPSHOT_SEASON` | ❌ | `2025/2026` | Label da temporada para os snapshots |

---

## Execução Local

```bash
# Instalar dependências
pip install -r requirements.txt

# Configurar variáveis de ambiente
cp .env.example .env
# editar .env com as credenciais

# Executar
python retrain.py
```

---

## Deploy no IBM Code Engine

```bash
# Build e push da imagem + criação/atualização do job
./deploy.sh
```

O script realiza:
1. `docker build` da imagem
2. `docker push` para o IBM Container Registry
3. Criação ou atualização do job no IBM Code Engine
4. Execução imediata do job via `ibmcloud ce jobrun submit`

---

## Dependências

| Pacote | Versão mínima | Uso |
|---|---|---|
| `pandas` | 2.0 | Manipulação de dados |
| `numpy` | 1.26 | Cálculos numéricos |
| `scipy` | 1.12 | Distribuições estatísticas |
| `joblib` | 1.3 | Serialização do modelo |
| `psycopg2-binary` | 2.9 | Conexão com Supabase (PostgreSQL) |
| `ibm-cos-sdk` | 2.13 | Upload para IBM Cloud Object Storage |

---

## Tabelas do Supabase

### Leitura
- `events` — partidas encerradas (`time_status = 3`)
- `teams` — nomes canônicos das equipes
- `match_stats` — dados de xG por partida

### Escrita (Snapshots)
- `team_player_strength_snapshot` — índices de força dos jogadores por equipe/temporada
- `team_style_snapshot_statsbomb` — métricas de estilo de jogo (StatsBomb)
- `player_absence_impact` — top-10 jogadores por impacto por equipe

---

## Critérios de Qualidade

- O job aborta se houver menos de **100 partidas** disponíveis para treino
- O enriquecimento Kaggle é **não-fatal**: se o CSV não existir, o treino prossegue sem ele
- A materialização de snapshots no Supabase também é **não-fatal**: falhas são logadas como `WARNING`
