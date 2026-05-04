class TrainDatasetQuery:
    def __init__(self, db_handler):
        self.db_handler = db_handler

    def train_dataset_df(self):
        # Portfolio note:
        # 실제 운영 쿼리는 회사 내부 테이블명을 사용하므로 공개 저장소에서는 샘플 스키마로 치환했습니다.
        query = """
        WITH daily_driver_volume AS (
            SELECT
                driver_id,
                DATE(CONVERT_TZ(box_assigned_at, '+00:00', '+09:00')) AS assign_date,
                COUNT(item_id) AS daily_volume
            FROM sample_delivery_items
            WHERE box_assigned_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)
            GROUP BY driver_id, assign_date
            HAVING daily_volume BETWEEN 5 AND 70
        ),
        ranked_deliveries AS (
            SELECT
                REGEXP_REPLACE(sector_code, '[0-9]', '') AS Area,
                route_id,
                delivery_completed_at AS timestamp_delivery_complete,
                LAG(delivery_completed_at) OVER (
                    PARTITION BY route_id
                    ORDER BY delivery_completed_at
                ) AS prev_timestamp
            FROM sample_delivery_items item
            JOIN daily_driver_volume volume
                ON item.driver_id = volume.driver_id
                AND DATE(CONVERT_TZ(item.box_assigned_at, '+00:00', '+09:00')) = volume.assign_date
            WHERE item.delivery_completed_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)
                AND item.status = 'DELIVERY_COMPLETE'
                AND item.delivery_completed_at IS NOT NULL
                AND item.delivery_failed_at IS NULL
                AND item.route_type != 'EXCLUDED'
        )
        SELECT
            Area,
            route_id AS shipping_container_id,
            timestamp_delivery_complete,
            WEEKDAY(CONVERT_TZ(timestamp_delivery_complete, '+00:00', '+09:00')) AS weekday,
            HOUR(CONVERT_TZ(timestamp_delivery_complete, '+00:00', '+09:00')) AS hour,
            TIMESTAMPDIFF(MINUTE, prev_timestamp, timestamp_delivery_complete) AS target_tasu
        FROM ranked_deliveries
        WHERE prev_timestamp IS NOT NULL
            AND HOUR(CONVERT_TZ(timestamp_delivery_complete, '+00:00', '+09:00')) IN (16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3);
        """
        return self.db_handler.fetch_data("sample_delivery_db", query, query_name="train_dataset_df")
