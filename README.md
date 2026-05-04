# 📦 Tasu Prediction Model Lambda

배송 완료 이력 데이터를 기반으로 지역·요일·시간대별 작업시간(`tasu`)을 예측하는 모델 학습 Lambda입니다.  
ETA 계산 Lambda에서 사용하는 작업시간 예측 모델을 주기적으로 학습하고, 모델 번들과 메타데이터를 S3에 저장하는 구조입니다.

---

## 📌 Overview

배송 ETA는 단순 이동시간만으로 계산하기 어렵습니다.  
실제 배송 현장에서는 기사별 정차, 하차, 이동, 물품 확인 등의 작업시간이 함께 반영되어야 합니다.

이 프로젝트는 최근 배송 완료 데이터를 학습 데이터로 구성하고, LightGBM 기반 회귀 모델을 학습하여 ETA 계산 시스템에서 사용할 `tasu` 예측값을 생성합니다.

---

## 🧠 Key Features

### 1. Daily Batch Training

- EventBridge Schedule 기반 매일 정해진 시간 자동 실행
- 최근 N일 배송 완료 데이터를 기준으로 학습 데이터셋 생성
- 모델 번들 및 학습 메타데이터를 S3에 저장

### 2. Tasu Prediction Model

- 지역 코드
- 요일
- 시간대
- 지역·시간대 평균 작업시간

위 피처를 기반으로 배송 건당 예상 작업시간을 예측합니다.

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

## 🏗 Architecture

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
S3 Model Artifact 저장
        ↓
ETA Calculate Lambda에서 최신 모델 참조
```

---

## 🗂 S3 Artifact Structure

아래 이미지는 학습 완료 후 S3에 저장되는 모델 아티팩트 구조 예시입니다.

![Tasu Model S3 Structure](docs/images/tasu_model_s3.png)

저장되는 주요 파일은 다음과 같습니다.

| File | Description |
|---|---|
| `model_bundle.joblib` | 학습된 모델, 피처 목록, 카테고리 피처, 평균값 fallback 테이블을 포함한 모델 번들 |
| `metadata.json` | 모델 버전, 학습 시각, 데이터셋 요약, 성능 지표, S3 Key 정보 |
| `latest.json` | ETA Lambda가 최신 모델 위치를 조회하기 위한 pointer 파일 |

---

## ⚙️ Tech Stack

- AWS Lambda Container Image
- AWS SAM
- Amazon EventBridge
- Amazon S3
- AWS Systems Manager Parameter Store
- Python
- Pandas
- LightGBM
- scikit-learn
- joblib

---

## 🧩 Model Logic

### Target

```text
target_tasu = previous_delivery_complete_time → current_delivery_complete_time 간 분 단위 차이
```

### Features

| Feature | Description |
|---|---|
| `Area` | 지역 코드에서 숫자를 제거한 지역 그룹 |
| `weekday` | 배송 완료 기준 요일 |
| `hour` | 배송 완료 기준 시간대 |
| `avg_time_per_sector_block` | 지역·시간대별 평균 작업시간 |

### Filtering

- 배송 완료 건만 사용
- 비정상 상태 데이터 제외
- 작업시간 0분 이하 / 과도하게 긴 값 제외
- 운영 시간대만 사용
- Q2~Q3 구간 중심으로 학습하여 극단값 영향 완화

---

## 📊 Training Output Example

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

## 🔄 Integration with ETA Lambda

이 모델은 단독 서비스가 아니라 ETA 계산 시스템의 하위 컴포넌트입니다.

```text
Tasu Predict Model Lambda → S3 latest.json → ETA Calculate Lambda → DynamoDB → TMS
```

ETA Calculate Lambda는 S3의 `latest.json`을 읽고, 최신 `model_bundle.joblib`을 로드하여 각 배송 stop별 작업시간을 예측합니다.

---

## 🚀 Deployment

```bash
sam build --no-cached
sam deploy --config-env prod
```

포트폴리오 저장소에서는 실제 계정, 버킷, VPC, SSM 경로를 제거하고 `samconfig.example.toml`만 제공합니다.

---

## 🔒 Security / Redaction

이 저장소는 포트폴리오 공개용으로 정리된 버전입니다.

제외 또는 샘플화한 항목:

- 실제 AWS Account ID
- 실제 S3 Bucket Name
- 실제 ECR Repository URI
- 실제 SSM Parameter Path
- 실제 DB Table Name
- 실제 회사 내부 데이터 스키마
- `.git`, `__pycache__`, 배포용 `samconfig.toml`

---

## 💡 Highlights

- ETA 정확도 개선을 위한 작업시간 예측 모델 설계
- EventBridge 기반 Daily Training 자동화
- 모델 아티팩트 버저닝 및 latest pointer 구조 구축
- Lambda 기반 경량 MLOps 파이프라인 구현
- ETA 계산 Lambda와 연계 가능한 모델 번들 구조 설계
