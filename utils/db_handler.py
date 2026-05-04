import time

import boto3
import pandas as pd
import pymysql

_SSM_CLIENT = boto3.client("ssm")
_DB_CONFIG_CACHE = None


# Portfolio note:
# 실제 서비스에서는 회사 계정의 SSM Parameter Store에서 DB 접속 정보를 조회했습니다.
# 공개 저장소에서는 경로를 샘플 값으로 치환했습니다.
def _load_db_config():
    global _DB_CONFIG_CACHE

    if _DB_CONFIG_CACHE is not None:
        return _DB_CONFIG_CACHE

    param_names = [
        "/sample/db/user",
        "/sample/db/password",
        "/sample/db/host",
        "/sample/db/database",
    ]

    response = _SSM_CLIENT.get_parameters(
        Names=param_names,
        WithDecryption=True,
    )

    params = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
    invalid = set(response.get("InvalidParameters", []))

    if invalid:
        raise ValueError(f"Invalid SSM parameter(s): {sorted(invalid)}")

    missing = [name for name in param_names if name not in params]
    if missing:
        raise ValueError(f"Missing SSM parameter(s): {missing}")

    _DB_CONFIG_CACHE = {
        "mysql_user": params["/sample/db/user"],
        "mysql_password": params["/sample/db/password"],
        "mysql_host": params["/sample/db/host"],
        "mysql_database": params["/sample/db/database"],
        "mysql_port": 3306,
    }
    return _DB_CONFIG_CACHE


class DBHandler:
    def __init__(self):
        cfg = _load_db_config()
        self.mysql_user = cfg["mysql_user"]
        self.mysql_password = cfg["mysql_password"]
        self.mysql_host = cfg["mysql_host"]
        self.mysql_database = cfg["mysql_database"]
        self.mysql_port = cfg["mysql_port"]

    def fetch_data(self, database, query, query_name=None):
        """Fetch query result as a pandas DataFrame."""
        query_label = query_name or "unknown_query"
        conn = None
        total_t0 = time.perf_counter()

        try:
            t0 = time.perf_counter()
            conn = pymysql.connect(
                host=self.mysql_host,
                user=self.mysql_user,
                passwd=self.mysql_password,
                db=database or self.mysql_database,
                charset="utf8",
                port=self.mysql_port,
                cursorclass=pymysql.cursors.DictCursor,
            )
            connect_ms = (time.perf_counter() - t0) * 1000

            with conn.cursor() as cur:
                t0 = time.perf_counter()
                cur.execute(query)
                execute_ms = (time.perf_counter() - t0) * 1000

                t0 = time.perf_counter()
                results = cur.fetchall()
                fetchall_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            df = pd.DataFrame(results)
            dataframe_ms = (time.perf_counter() - t0) * 1000

            total_ms = (time.perf_counter() - total_t0) * 1000
            print(
                f"[perf][db] query_name={query_label} "
                f"connect_ms={connect_ms:.1f} "
                f"execute_ms={execute_ms:.1f} "
                f"fetchall_ms={fetchall_ms:.1f} "
                f"dataframe_ms={dataframe_ms:.1f} "
                f"total_ms={total_ms:.1f} "
                f"rows={len(df)}"
            )
            return df

        except Exception as e:
            total_ms = (time.perf_counter() - total_t0) * 1000
            print(f"[perf][db] query_name={query_label} total_ms={total_ms:.1f} error={repr(e)}")
            return None

        finally:
            if conn is not None:
                conn.close()
