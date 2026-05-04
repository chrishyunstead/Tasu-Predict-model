import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

import boto3
import joblib
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    mean_squared_error,
    r2_score,
)

from query.traindata import TrainDatasetQuery
from utils.db_handler import DBHandler

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_CLIENT = boto3.client("s3")
KST = timezone(timedelta(hours=9))

ALLOWED_HOURS = [16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3]
HOUR_MAPPING = {
    16: "오후4-5시",
    17: "오후5-6시",
    18: "오후6-7시",
    19: "오후7-8시",
    20: "오후8-9시",
    21: "오후9-10시",
    22: "오후10-11시",
    23: "오후11-12시",
    0: "오전12시-01시",
    1: "오전01시-02시",
    2: "오전02시-03시",
    3: "오전03시-04시",
}
CATEGORICAL_FEATURES = ["Area", "weekday", "hour"]
FEATURE_COLUMNS = ["Area", "weekday", "hour", "avg_time_per_sector_block"]
TARGET_COLUMN = "target_tasu"


def _validate_raw_df(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        raise ValueError("Training dataset is empty.")

    required_cols = [
        "timestamp_delivery_complete",
        "Area",
        "weekday",
        "hour",
        TARGET_COLUMN,
    ]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")


def preprocess_training_data(df: pd.DataFrame) -> pd.DataFrame:
    work_df = df.copy()
    _validate_raw_df(work_df)

    work_df["timestamp_delivery_complete"] = pd.to_datetime(
        work_df["timestamp_delivery_complete"], errors="coerce"
    )

    before = len(work_df)
    work_df = work_df.dropna(
        subset=["timestamp_delivery_complete", "Area", "weekday", "hour", TARGET_COLUMN]
    ).copy()
    logger.info("[train] dropna: %s -> %s", before, len(work_df))

    work_df[TARGET_COLUMN] = pd.to_numeric(work_df[TARGET_COLUMN], errors="coerce")
    work_df["weekday"] = pd.to_numeric(work_df["weekday"], errors="coerce")
    work_df["hour"] = pd.to_numeric(work_df["hour"], errors="coerce")

    before = len(work_df)
    work_df = work_df[
        (work_df[TARGET_COLUMN] > 0)
        & (work_df[TARGET_COLUMN] <= 20)
        & (work_df["hour"].isin(ALLOWED_HOURS))
        & (work_df["weekday"].between(0, 6))
    ].copy()
    logger.info("[train] basic filter: %s -> %s", before, len(work_df))

    q1 = work_df[TARGET_COLUMN].quantile(0.25)
    q2 = work_df[TARGET_COLUMN].quantile(0.50)
    q3 = work_df[TARGET_COLUMN].quantile(0.75)

    def assign_quartile(value: float) -> str:
        if value <= q1:
            return "Q1"
        if value <= q2:
            return "Q2"
        if value <= q3:
            return "Q3"
        return "Q4"

    work_df["target_quartile"] = work_df[TARGET_COLUMN].apply(assign_quartile)

    before = len(work_df)
    work_df = work_df[work_df["target_quartile"].isin(["Q2", "Q3"])].copy()
    logger.info("[train] quartile filter(Q2/Q3): %s -> %s", before, len(work_df))

    work_df = work_df.sort_values("timestamp_delivery_complete").reset_index(drop=True)
    work_df["hour"] = work_df["hour"].astype(int)
    work_df["weekday"] = work_df["weekday"].astype(int)
    work_df["time_block"] = work_df["hour"].map(HOUR_MAPPING)
    return work_df


def build_avg_features(
    train_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, float]:
    sector_time_avg = (
        train_df.groupby(["Area", "time_block"], observed=True)[TARGET_COLUMN]
        .mean()
        .reset_index()
        .rename(columns={TARGET_COLUMN: "avg_time_per_sector_block"})
    )

    sector_avg = (
        train_df.groupby(["Area"], observed=True)[TARGET_COLUMN]
        .mean()
        .reset_index()
        .rename(columns={TARGET_COLUMN: "sector_avg_tasu"})
    )

    timeblock_avg = (
        train_df.groupby(["time_block"], observed=True)[TARGET_COLUMN]
        .mean()
        .reset_index()
        .rename(columns={TARGET_COLUMN: "timeblock_avg_tasu"})
    )

    global_avg = float(train_df[TARGET_COLUMN].mean())
    return sector_time_avg, sector_avg, timeblock_avg, global_avg


def attach_avg_features(
    base_df: pd.DataFrame,
    sector_time_avg: pd.DataFrame,
    sector_avg: pd.DataFrame,
    timeblock_avg: pd.DataFrame,
    global_avg: float,
) -> pd.DataFrame:
    out = base_df.copy()
    out = out.merge(sector_time_avg, on=["Area", "time_block"], how="left")
    out = out.merge(sector_avg, on=["Area"], how="left")
    out = out.merge(timeblock_avg, on=["time_block"], how="left")
    out["avg_time_per_sector_block"] = out["avg_time_per_sector_block"].fillna(out["sector_avg_tasu"])
    out["avg_time_per_sector_block"] = out["avg_time_per_sector_block"].fillna(out["timeblock_avg_tasu"])
    out["avg_time_per_sector_block"] = out["avg_time_per_sector_block"].fillna(global_avg)
    return out


def time_split(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * 0.7)
    valid_end = int(n * 0.8)

    train_df = df.iloc[:train_end].copy()
    valid_df = df.iloc[train_end:valid_end].copy()
    test_df = df.iloc[valid_end:].copy()

    if train_df.empty or valid_df.empty or test_df.empty:
        raise ValueError(
            f"Time split produced empty dataset. train={len(train_df)}, valid={len(valid_df)}, test={len(test_df)}"
        )

    return train_df, valid_df, test_df


def train_model(df: pd.DataFrame) -> Dict:
    train_df, valid_df, test_df = time_split(df)
    sector_time_avg, sector_avg, timeblock_avg, global_avg = build_avg_features(train_df)

    train_df = attach_avg_features(train_df, sector_time_avg, sector_avg, timeblock_avg, global_avg)
    valid_df = attach_avg_features(valid_df, sector_time_avg, sector_avg, timeblock_avg, global_avg)
    test_df = attach_avg_features(test_df, sector_time_avg, sector_avg, timeblock_avg, global_avg)

    for part_df in [train_df, valid_df, test_df]:
        for col in CATEGORICAL_FEATURES:
            part_df[col] = part_df[col].astype("category")

    X_train = train_df[FEATURE_COLUMNS]
    y_train = train_df[TARGET_COLUMN]
    X_valid = valid_df[FEATURE_COLUMNS]
    y_valid = valid_df[TARGET_COLUMN]
    X_test = test_df[FEATURE_COLUMNS]
    y_test = test_df[TARGET_COLUMN]

    model = lgb.LGBMRegressor(
        objective="regression",
        metric="rmse",
        n_estimators=3000,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=8,
        min_child_samples=30,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.5,
        reg_lambda=0.5,
        random_state=42,
        force_col_wise=True,
    )

    callbacks = [
        lgb.early_stopping(stopping_rounds=100, verbose=True),
        lgb.log_evaluation(period=100),
    ]

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_train, y_train), (X_valid, y_valid)],
        eval_names=["train", "valid"],
        eval_metric="rmse",
        categorical_feature=CATEGORICAL_FEATURES,
        callbacks=callbacks,
    )

    best_iter = model.best_iteration_ if getattr(model, "best_iteration_", None) else model.n_estimators_

    y_pred_train = model.predict(X_train, num_iteration=best_iter)
    y_pred_valid = model.predict(X_valid, num_iteration=best_iter)
    y_pred_test = model.predict(X_test, num_iteration=best_iter)

    metrics = {
        "train_rmse": float(mean_squared_error(y_train, y_pred_train, squared=False)),
        "valid_rmse": float(mean_squared_error(y_valid, y_pred_valid, squared=False)),
        "test_rmse": float(mean_squared_error(y_test, y_pred_test, squared=False)),
        "train_mae": float(mean_absolute_error(y_train, y_pred_train)),
        "valid_mae": float(mean_absolute_error(y_valid, y_pred_valid)),
        "test_mae": float(mean_absolute_error(y_test, y_pred_test)),
        "train_mape": float(mean_absolute_percentage_error(y_train, y_pred_train) * 100),
        "valid_mape": float(mean_absolute_percentage_error(y_valid, y_pred_valid) * 100),
        "test_mape": float(mean_absolute_percentage_error(y_test, y_pred_test) * 100),
        "train_r2": float(r2_score(y_train, y_pred_train)),
        "valid_r2": float(r2_score(y_valid, y_pred_valid)),
        "test_r2": float(r2_score(y_test, y_pred_test)),
        "under_10": float((((y_test - y_pred_test).abs() / y_test) * 100 <= 10).mean() * 100),
        "under_30": float((((y_test - y_pred_test).abs() / y_test) * 100 <= 30).mean() * 100),
    }

    artifact_bundle = {
        "model": model,
        "best_iteration": int(best_iter),
        "feature_columns": FEATURE_COLUMNS,
        "categorical_features": CATEGORICAL_FEATURES,
        "hour_mapping": HOUR_MAPPING,
        "avg_feature_tables": {
            "sector_time_avg": sector_time_avg,
            "sector_avg": sector_avg,
            "timeblock_avg": timeblock_avg,
            "global_avg": global_avg,
        },
    }

    split_summary = {
        "train_rows": int(len(train_df)),
        "valid_rows": int(len(valid_df)),
        "test_rows": int(len(test_df)),
        "train_min_ts": str(train_df["timestamp_delivery_complete"].min()),
        "train_max_ts": str(train_df["timestamp_delivery_complete"].max()),
        "valid_min_ts": str(valid_df["timestamp_delivery_complete"].min()),
        "valid_max_ts": str(valid_df["timestamp_delivery_complete"].max()),
        "test_min_ts": str(test_df["timestamp_delivery_complete"].min()),
        "test_max_ts": str(test_df["timestamp_delivery_complete"].max()),
    }

    return {
        "artifact_bundle": artifact_bundle,
        "metrics": metrics,
        "split_summary": split_summary,
    }


def upload_json(bucket: str, key: str, payload: Dict) -> None:
    S3_CLIENT.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def upload_model_artifact(bucket: str, key: str, artifact_bundle: Dict) -> None:
    with tempfile.NamedTemporaryFile(suffix=".joblib") as tmp_file:
        joblib.dump(artifact_bundle, tmp_file.name)
        tmp_file.flush()
        S3_CLIENT.upload_file(tmp_file.name, bucket, key)


def lambda_handler(event, context):
    logger.info("[lambda] event=%s", json.dumps(event or {}, ensure_ascii=False))

    bucket = os.environ["MODEL_S3_BUCKET"]
    prefix = os.environ.get("MODEL_S3_PREFIX", "tasu-predict").strip("/")

    now_kst = datetime.now(KST)
    model_version = now_kst.strftime("%Y%m%dT%H%M%S")

    db_handler = DBHandler()
    query = TrainDatasetQuery(db_handler)
    raw_df = query.train_dataset_df()

    processed_df = preprocess_training_data(raw_df)
    result = train_model(processed_df)

    model_key = f"{prefix}/artifacts/{model_version}/model_bundle.joblib"
    metadata_key = f"{prefix}/artifacts/{model_version}/metadata.json"
    latest_key = f"{prefix}/latest.json"

    upload_model_artifact(bucket, model_key, result["artifact_bundle"])

    metadata = {
        "model_version": model_version,
        "trained_at_kst": now_kst.isoformat(),
        "s3_bucket": bucket,
        "model_key": model_key,
        "target": TARGET_COLUMN,
        "feature_columns": FEATURE_COLUMNS,
        "categorical_features": CATEGORICAL_FEATURES,
        "allowed_hours": ALLOWED_HOURS,
        "filters": {
            "target_min_exclusive": 0,
            "target_max_inclusive": 20,
            "quartiles_kept": ["Q2", "Q3"],
        },
        "dataset": {
            "raw_rows": int(len(raw_df)),
            "processed_rows": int(len(processed_df)),
            "unique_sector_count": int(processed_df["Area"].nunique()),
        },
        "split_summary": result["split_summary"],
        "metrics": result["metrics"],
    }

    upload_json(bucket, metadata_key, metadata)
    upload_json(
        bucket,
        latest_key,
        {
            "model_version": model_version,
            "trained_at_kst": now_kst.isoformat(),
            "s3_bucket": bucket,
            "model_key": model_key,
            "metadata_key": metadata_key,
        },
    )

    response = {
        "success": True,
        "model_version": model_version,
        "bucket": bucket,
        "model_key": model_key,
        "metadata_key": metadata_key,
        "latest_key": latest_key,
        "metrics": result["metrics"],
        "dataset": metadata["dataset"],
    }
    logger.info("[lambda] response=%s", json.dumps(response, ensure_ascii=False, default=str))
    return response
