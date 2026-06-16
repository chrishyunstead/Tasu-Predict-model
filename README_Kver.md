# 📦 Tasu Prediction Model Lambda

## ETA 계산을 위한 아이템별 작업시간(tasu) 예측 모델 학습 파이프라인

배송 완료 이력 데이터를 기반으로 **하나의 아이템을 배송 처리하는 데 걸리는 작업시간(`tasu`)**을 예측하는 모델 학습 Lambda입니다.  
ETA 계산 Lambda에서 사용하는 작업시간 예측 모델을 주기적으로 학습하고, 모델 번들과 메타데이터를 S3에 저장하는 서버리스 MLOps 파이프라인입니다.

> 여기서 `tasu`는 단순 이동시간이 아니라, 한 지점에서 다음 지점으로 이동한 뒤 하나의 아이템을 배송 처리하는 데 걸리는 작업시간을 의미합니다.  
> ETA 계산 시 OSRM 이동시간만으로 설명하기 어려운 정차, 하차, 물품 확인, 배송 처리 시간을 보완하기 위해 사용합니다.

---

## Executive Impact

| Area | Before | After |
|---|---|---|
| ETA 작업시간 | 고정값/단순 평균 의존 | 지역·요일·시간대 기반 ML 예측 |
| 모델 관리 | 수동 파일 관리 | S3 artifact versioning |
| 최신 모델 참조 | 직접 경로 지정 필요 | `latest.json` pointer 사용 |
| 운영 방식 | 단발성 학습 | EventBridge 기반 Daily Training |

---

## Business Problem

배송 ETA는 단순 이동시간만으로 계산하기 어렵습니다.  
실제 배송 현장에서는 이동 이후에도 다음 작업시간이 발생합니다.

- 정차 및 하차
- 물품 확인
- 공동현관/엘리베이터 이동
- 수령지 도착 후 전달
- 동일 주소 복수 아이템 처리

이 작업시간은 지역, 요일, 시간대, 배송 패턴에 따라 달라지기 때문에 고정값으로 처리하면 ETA 오차가 커질 수 있습니다.

---

## My Role

- ETA 계산에 사용할 작업시간 target 정의
- 최근 배송 완료 이력 기반 학습 데이터셋 구성
- 지역·요일·시간대 feature engineering 구현
- LightGBM 기반 회귀 모델 학습 파이프라인 구현
- 모델 번들, metadata, latest pointer를 S3에 저장하는 artifact versioning 구조 설계
- ETA Lambda에서 최신 모델을 로드할 수 있는 연동 구조 설계

---

## Key Features

### 1. Daily Batch Training

- EventBridge Schedule 기반 매일 정해진 시간 자동 실행
- 최근 N일 배송 완료 데이터를 기준으로 학습 데이터셋 생성
- 모델 번들 및 학습 메타데이터를 S3에 저장

### 2. Tasu Prediction Model

다음 피처를 기반으로 아이템별 예상 작업시간을 예측합니다.

| Feature | Description |
|---|---|
| `Area` | 지역 코드에서 숫자를 제거한 지역 그룹 |
| `weekday` | 배송 완료 기준 요일 |
| `hour` | 배송 완료 기준 시간대 |
| `avg_time_per_sector_block` | 지역·시간대별 평균 작업시간 |

### 3. Artifact Versioning

학습이 완료될 때마다 timestamp 기반 버전 디렉터리에 모델을 저장합니다.

```text
s3://<model-bucket>/tasu-predict/artifacts/{model_version}/
├── model_bundle.joblib
└── metadata.json
```

### 4. Latest Pointer

ETA 계산 Lambda가 항상 최신 모델을 사용할 수 있도록 `latest.json`을 별도로 저장합니다.

```text
s3://<model-bucket>/tasu-predict/latest.json
```

---

## Architecture

```text
EventBridge Schedule
        ↓
Tasu Predict Train Lambda
        ↓
Training Dataset Query
        ↓
Preprocessing / Feature Engineering
        ↓
LightGBM Model Training
        ↓
S3 Model Artifact Save
        ↓
latest.json Update
        ↓
ETA Calculate Lambda에서 최신 모델 참조
```

---

## Model Logic

### Target

```text
target_tasu = previous_delivery_complete_time → current_delivery_complete_time 간 분 단위 차이
```

이 값은 한 지점에서 다음 지점으로 이동한 뒤 하나의 아이템을 배송 처리하는 데 걸리는 작업시간을 근사합니다.

### Filtering

- 배송 완료 건만 사용
- 비정상 상태 데이터 제외
- 작업시간 0분 이하 / 과도하게 긴 값 제외
- 운영 시간대만 사용
- Q2~Q3 구간 중심으로 학습하여 극단값 영향 완화

---

## S3 Artifact Structure

![Tasu Model S3 Structure](docs/images/tasu_model_s3.png)

| File | Description |
|---|---|
| `model_bundle.joblib` | 학습된 모델, 피처 목록, 카테고리 피처, 평균값 fallback 테이블을 포함한 모델 번들 |
| `metadata.json` | 모델 버전, 학습 시각, 데이터셋 요약, 성능 지표, S3 Key 정보 |
| `latest.json` | ETA Lambda가 최신 모델 위치를 조회하기 위한 pointer 파일 |

---

## Training Output Example

```json
{
  "success": true,
  "model_version": "20260504T120008",
  "model_key": "tasu-predict/artifacts/20260504T120008/model_bundle.joblib",
  "metadata_key": "tasu-predict/artifacts/20260504T120008/metadata.json",
  "latest_key": "tasu-predict/latest.json",
  "metrics": {
    "test_rmse": 1.01,
    "test_mae": 0.72,
    "test_mape": 13.5,
    "test_r2": 0.83
  }
}
```

---

## Integration with ETA Lambda

이 모델은 단독 서비스가 아니라 ETA 계산 시스템의 upstream 모델입니다.

```text
Tasu Predict Model Lambda
        ↓
S3 latest.json
        ↓
ETA Calculate Lambda
        ↓
DynamoDB
        ↓
TMS
```

ETA Calculate Lambda는 S3의 `latest.json`을 읽고 최신 `model_bundle.joblib`을 로드하여 각 배송 stop별 작업시간을 예측합니다.

---

## Tech Stack

| Category | Stack |
|---|---|
| Compute | AWS Lambda Container Image |
| Infra | AWS SAM |
| Scheduler | Amazon EventBridge |
| Storage | Amazon S3 |
| Secrets | AWS Systems Manager Parameter Store |
| Language | Python |
| Data | Pandas |
| ML | LightGBM, Scikit-learn |
| Serialization | joblib |

---

## Deployment

```bash
sam build --no-cached
sam deploy --config-env prod
```

포트폴리오 저장소에서는 실제 계정, 버킷, VPC, SSM 경로를 제거하고 `samconfig.example.toml`만 제공합니다.

---

## Security / Redaction

포트폴리오 공개를 위해 다음 항목은 제거하거나 샘플 값으로 대체했습니다.

- 실제 AWS Account ID
- 실제 S3 Bucket Name
- 실제 ECR Repository URI
- 실제 SSM Parameter Path
- 실제 DB Table Name
- 실제 회사 내부 데이터 스키마
- `.git`, `__pycache__`, 배포용 `samconfig.toml`

---

## Key Takeaway

> ETA 정확도 향상을 위해 아이템별 작업시간(tasu) 예측 모델을 설계하고,  
> EventBridge + Lambda + S3 artifact versioning 기반의 경량 MLOps 학습 파이프라인으로 운영했습니다.
