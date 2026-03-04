import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import requests


ENDPOINTS: Dict[str, str] = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
}

HEADER_KEYS: List[str] = ["AUTH_KEY", "AUTH-KEY", "auth_key"]
DATE_KEYS: List[str] = ["basDd", "basDt", "trdDd", "date"]
REQUEST_MODES: List[str] = ["GET", "POST_DATA", "POST_JSON"]


def previous_business_day(today: datetime) -> str:
    dt = today.date() - timedelta(days=1)
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt.strftime("%Y%m%d")


def call_endpoint(
    session: requests.Session,
    url: str,
    header_key: str,
    api_key: str,
    date_key: str,
    date_value: str,
    mode: str,
) -> Tuple[int, str, str]:
    headers = {header_key: api_key}
    payload = {date_key: date_value}

    if mode == "GET":
        resp = session.get(url, headers=headers, params=payload, timeout=10)
    elif mode == "POST_DATA":
        resp = session.post(url, headers=headers, data=payload, timeout=10)
    else:
        resp = session.post(url, headers=headers, json=payload, timeout=10)

    body_preview = resp.text.replace("\r", " ").replace("\n", " ")[:140]
    return resp.status_code, resp.headers.get("content-type", ""), body_preview


def main() -> int:
    api_key = os.getenv("KRX_API_KEY", "").strip()
    if not api_key:
        print("[probe] KRX_API_KEY is missing")
        return 1

    target_date = os.getenv("KRX_PROBE_DATE", "").strip() or previous_business_day(datetime.now())
    print(f"[probe] target_date={target_date} key_length={len(api_key)}")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    success_found = False

    for market, url in ENDPOINTS.items():
        print(f"[probe] market={market} url={url}")
        for header_key in HEADER_KEYS:
            for date_key in DATE_KEYS:
                for mode in REQUEST_MODES:
                    try:
                        status, ctype, preview = call_endpoint(
                            session=session,
                            url=url,
                            header_key=header_key,
                            api_key=api_key,
                            date_key=date_key,
                            date_value=target_date,
                            mode=mode,
                        )
                        print(
                            f"[probe] {mode:9} {header_key:8} {date_key:6} -> {status} "
                            f"ct={ctype} body={preview}"
                        )
                        if status == 200:
                            success_found = True
                    except Exception as e:
                        print(f"[probe] {mode:9} {header_key:8} {date_key:6} -> EXCEPTION {e}")

    if success_found:
        print("[probe] At least one request succeeded with 200")
        return 0

    print("[probe] No successful 200 response from KRX OpenAPI")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
