#!/usr/bin/env python3
"""
Scout — Weekly Poisson Model Retraining
========================================
Pulls finished match data from Supabase, trains the Poisson model,
and uploads the serialized artifact to Azure Blob Storage.

Designed to run as an Azure Container Apps Job on a weekly cron schedule.

Environment variables required:
    SUPABASE_DB_URL               — Supabase PostgreSQL connection string
    AZURE_STORAGE_CONNECTION_STRING — Azure Blob Storage connection string
    AZURE_STORAGE_CONTAINER       — Blob container name (default: "models")
    MODEL_BLOB_NAME               — Blob file name (default: "poisson_model.pkl")
"""

import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import joblib
import pandas as pd
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONTAINER   = os.getenv("AZURE_STORAGE_CONTAINER", "models")
BLOB_NAME   = os.getenv("MODEL_BLOB_NAME", "poisson_model.pkl")
CARD_BLOB   = os.getenv("MODEL_CARD_BLOB_NAME", "model_card.json")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_training_data() -> pd.DataFrame:
    """Pull finished Premier League matches from Supabase."""
    db_url = os.environ["SUPABASE_DB_URL"]
    logger.info("Connecting to Supabase …")
    conn = psycopg2.connect(db_url)

    query = """
        SELECT
            e.id          AS event_id,
            ht.name       AS home_team_name,
            at.name       AS away_team_name,
            e.home_score,
            e.away_score,
            e.time_utc
        FROM events e
        JOIN teams ht ON ht.id = e.home_team_id
        JOIN teams at ON at.id = e.away_team_id
        WHERE e.time_status = 3          -- ended only
          AND e.home_score IS NOT NULL
          AND e.away_score IS NOT NULL
        ORDER BY e.time_utc ASC
    """
    df = pd.read_sql(query, conn)
    conn.close()
    logger.info("Loaded %d finished matches from Supabase", len(df))
    return df


# ── Training ──────────────────────────────────────────────────────────────────

def train(df: pd.DataFrame) -> tuple[dict, dict]:
    """
    Fit the Poisson model and return (model_data, model_card).
    Identical algorithm to scripts/train_model.py — single source of truth.
    """
    df = df.copy()
    df["home_score"] = pd.to_numeric(df["home_score"], errors="coerce")
    df["away_score"] = pd.to_numeric(df["away_score"], errors="coerce")
    df = df.dropna(subset=["home_score", "away_score"])
    n = len(df)

    league_avg_home = df["home_score"].mean()
    league_avg_away = df["away_score"].mean()
    league_avg_total = (league_avg_home + league_avg_away) / 2

    logger.info(
        "Training on %d matches | avg goals home=%.3f away=%.3f",
        n, league_avg_home, league_avg_away,
    )

    home_stats = df.groupby("home_team_name").agg(
        home_goals_scored=("home_score", "sum"),
        home_goals_conceded=("away_score", "sum"),
        home_matches=("home_score", "count"),
    )
    away_stats = df.groupby("away_team_name").agg(
        away_goals_scored=("away_score", "sum"),
        away_goals_conceded=("home_score", "sum"),
        away_matches=("away_score", "count"),
    )

    all_teams = sorted(set(home_stats.index) | set(away_stats.index))
    team_strengths: dict[str, dict] = {}

    for team in all_teams:
        h = home_stats.loc[team] if team in home_stats.index else None
        a = away_stats.loc[team] if team in away_stats.index else None

        scored = conceded = matches = 0.0
        if h is not None:
            scored   += float(h["home_goals_scored"])
            conceded += float(h["home_goals_conceded"])
            matches  += int(h["home_matches"])
        if a is not None:
            scored   += float(a["away_goals_scored"])
            conceded += float(a["away_goals_conceded"])
            matches  += int(a["away_matches"])

        if matches == 0:
            attack, defense = 1.0, 1.0
        else:
            attack  = max((scored  / matches) / league_avg_total, 0.1)
            defense = max((conceded / matches) / league_avg_total, 0.1)

        team_strengths[team] = {
            "attack":  round(attack, 4),
            "defense": round(defense, 4),
        }

    model_data = {
        "team_strengths":         team_strengths,
        "league_avg_home_goals":  round(league_avg_home, 6),
        "league_avg_away_goals":  round(league_avg_away, 6),
        "n_matches":              n,
        "fitted":                 True,
        "trained_at":             datetime.now(timezone.utc).isoformat(),
    }

    date_min = str(df["time_utc"].min())[:10] if "time_utc" in df.columns else "unknown"
    date_max = str(df["time_utc"].max())[:10] if "time_utc" in df.columns else "unknown"

    model_card = {
        "model_name":     "Poisson Match Predictor",
        "version":        "2.0.0",
        "algorithm":      "Independent Poisson Goals (Dixon-Coles inspired)",
        "training_source": "Supabase (live)",
        "training_matches": n,
        "date_range":     {"from": date_min, "to": date_max},
        "league":         "England Premier League (BetsAPI ID: 94)",
        "league_averages": {
            "home_goals_per_match":  round(league_avg_home, 4),
            "away_goals_per_match":  round(league_avg_away, 4),
            "total_goals_per_match": round(league_avg_home + league_avg_away, 4),
        },
        "teams_trained": len(all_teams),
        "trained_at":    model_data["trained_at"],
    }

    logger.info("Model trained — %d teams", len(all_teams))
    return model_data, model_card


# ── Azure Blob upload ─────────────────────────────────────────────────────────

def upload_to_blob(model_data: dict, model_card: dict) -> None:
    from azure.storage.blob import BlobServiceClient

    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    client   = BlobServiceClient.from_connection_string(conn_str)
    container_client = client.get_container_client(CONTAINER)

    # Ensure container exists
    try:
        container_client.create_container()
        logger.info("Created blob container '%s'", CONTAINER)
    except Exception:
        pass  # already exists

    # Upload model.pkl
    buffer = io.BytesIO()
    joblib.dump(model_data, buffer, compress=3)
    buffer.seek(0)
    container_client.upload_blob(BLOB_NAME, buffer, overwrite=True)
    logger.info("Uploaded %s to blob container '%s'", BLOB_NAME, CONTAINER)

    # Upload model_card.json
    card_bytes = json.dumps(model_card, indent=2, ensure_ascii=False).encode()
    container_client.upload_blob(CARD_BLOB, card_bytes, overwrite=True)
    logger.info("Uploaded %s", CARD_BLOB)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.monotonic()
    logger.info("=== Scout Retraining Job started ===")

    df = load_training_data()
    if len(df) < 100:
        logger.error("Too few matches (%d) to train reliably. Aborting.", len(df))
        sys.exit(1)

    model_data, model_card = train(df)
    upload_to_blob(model_data, model_card)

    elapsed = time.monotonic() - t0
    logger.info("=== Retraining complete in %.1fs ===", elapsed)


if __name__ == "__main__":
    main()
