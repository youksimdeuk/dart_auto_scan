"""
주식 자동화 스캐너
- 전종목 모니터링 (KOSPI, KOSDAQ)
- 3가지 조건 필터링: 거래량 2.5배↑, 주가 4%↓, 외국인순매수+
- 매일 오후 4시 자동 실행
- 텔레그램 메시지 발송
- GitHub Actions에서 자동 실행
"""

import pandas as pd
import requests
import json
import os
import socket
import re
from io import StringIO
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import time
from pykrx import stock as pykrx_stock

# pykrx 내부 HTTP 요청 전역 타임아웃 (네트워크 hanging 방지)
socket.setdefaulttimeout(15)

# pykrx 내부의 잘못된 logging.info(args, kwargs) 호출로 인한 포맷 예외 방지
class SafeLogRecord(logging.LogRecord):
    def getMessage(self) -> str:
        msg = str(self.msg)
        if self.args:
            try:
                msg = msg % self.args
            except Exception:
                # 포맷이 깨진 로그는 원문 메시지만 사용
                return msg
        return msg


class DropPykrxUtilNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        path = str(getattr(record, "pathname", "")).replace("/", "\\")
        return "pykrx\\website\\comm\\util.py" not in path


logging.setLogRecordFactory(SafeLogRecord)

# 로깅 설정
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL_VALUE = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL_VALUE,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

root_logger = logging.getLogger()
for handler in root_logger.handlers:
    handler.addFilter(DropPykrxUtilNoise())

# 민감정보(토큰 포함 URL) 노출 방지를 위해 HTTP 디버그 로그는 억제
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


# ============================================================================
# 설정 (CONFIG)
# ============================================================================

class Config:
    """설정 클래스"""
    # 텔레그램 토큰, 채팅 ID (환경변수에서 읽기)
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', "YOUR_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', "YOUR_CHAT_ID")

    # KRX 인증/조회 설정
    KRX_API_KEY = os.getenv('KRX_API_KEY', '')
    KRX_USER_ID = os.getenv('KRX_USER_ID', '')
    KRX_PASSWORD = os.getenv('KRX_PASSWORD', '')
    
    # 필터 조건
    VOLUME_RATIO = 2.5                      # 거래량 배수
    PRICE_DROP_PCT = -4.0                   # 주가 하락률 (%)

    # API 설정
    REQUEST_TIMEOUT = 10                    # 텔레그램 API 타임아웃 (초)
    KRX_API_TIMEOUT = 10                    # KRX API 타임아웃 (초)
    KRX_OPENAPI_RETRY = 2                   # KRX OpenAPI 요청 재시도 횟수
    USE_PYKRX_FALLBACK = env_flag('USE_PYKRX_FALLBACK', False)

    # 실행 설정
    RUN_TIME = "18:00"                      # 오후 6시 (한국 기준)


# ============================================================================
# 데이터 수집 (DATA FETCHER)
# ============================================================================

class SessionRequestsAdapter:
    """pykrx webio.requests 대체용 어댑터 (requests-like interface)"""

    def __init__(self, session: requests.Session, timeout: int):
        self._session = session
        self._timeout = timeout

    def get(self, url: str, **kwargs):
        kwargs.setdefault('timeout', self._timeout)
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        kwargs.setdefault('timeout', self._timeout)
        return self._session.post(url, **kwargs)


class KrxSessionAuth:
    """data.krx.co.kr 로그인 세션 확보 및 pykrx 주입"""

    LOGIN_URL_CANDIDATES = [
        "https://data.krx.co.kr/comm/authService/login/login.do",
        "https://data.krx.co.kr/comm/login/login.do",
        "https://data.krx.co.kr/comm/bldAttendant/login/login.do",
        "https://data.krx.co.kr/comm/user/login/login.do",
    ]

    def __init__(self, user_id: str, password: str, timeout: int = 10):
        self.user_id = user_id
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
        })

    def _is_login_success(self, response: requests.Response) -> bool:
        if response.status_code != 200:
            return False

        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ('success', 'isSuccess'):
                    if payload.get(key) is True:
                        return True
                code = str(payload.get('code', payload.get('resultCode', payload.get('resultCd', '')))).upper()
                if code in ('0', '00', 'S', 'SUCCESS'):
                    return True
        except ValueError:
            pass

        if self.session.cookies:
            text = response.text.lower()
            fail_markers = ('fail', 'error', 'invalid', '로그인 실패')
            return not any(marker in text for marker in fail_markers)

        return False

    def login(self) -> bool:
        if not self.user_id or not self.password:
            logger.warning("KRX 로그인 계정이 없어 외국인 순매수 조회를 비활성화합니다.")
            return False

        # 세션 쿠키 초기화
        try:
            self.session.get(
                "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
                timeout=self.timeout
            )
        except Exception as e:
            logger.debug(f"KRX 로그인 사전 요청 실패(무시): {e}")

        payload_candidates = [
            {"loginId": self.user_id, "loginPw": self.password},
            {"userId": self.user_id, "userPw": self.password},
            {"user_id": self.user_id, "user_pw": self.password},
            {"mbrId": self.user_id, "mbrPw": self.password},
            {"usrId": self.user_id, "usrPwd": self.password},
            {"id": self.user_id, "password": self.password},
        ]

        for login_url in self.LOGIN_URL_CANDIDATES:
            for payload in payload_candidates:
                try:
                    resp = self.session.post(login_url, data=payload, timeout=self.timeout)
                    if self._is_login_success(resp):
                        logger.info(f"KRX 로그인 성공 ({login_url})")
                        return True
                except Exception as e:
                    logger.debug(f"KRX 로그인 시도 실패 ({login_url}): {e}")

        logger.warning("KRX 로그인 실패: 외국인 순매수 조회를 건너뜁니다.")
        return False

    def inject_to_pykrx(self) -> bool:
        try:
            from pykrx.website.comm import webio
            webio.requests = SessionRequestsAdapter(self.session, self.timeout)
            logger.info("pykrx 요청 경로에 KRX 인증 세션을 주입했습니다.")
            return True
        except Exception as e:
            logger.warning(f"pykrx 세션 주입 실패: {e}")
            return False


class KrxOpenApiFetcher:
    """KRX OpenAPI를 통한 전종목 OHLCV bulk 조회"""

    ENDPOINTS = {
        'KOSPI': [
            "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
            "http://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
        ],
        'KOSDAQ': [
            "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
            "http://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
        ],
    }
    REQUEST_PROFILES = (
        ("GET", "AUTH_KEY", "basDd"),
        ("GET", "auth_key", "basDd"),
        ("POST_DATA", "AUTH_KEY", "basDd"),
        ("POST_JSON", "AUTH_KEY", "basDd"),
        ("GET", "AUTH_KEY", "trdDd"),
        ("GET", "auth_key", "trdDd"),
    )

    def __init__(self, api_key: str, timeout: int = 10):
        self.api_key = (api_key or '').strip()
        self.timeout = timeout
        self._working_profiles: Dict[str, Tuple[str, str, str]] = {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

    @staticmethod
    def _to_int(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip().replace(',', '')
        if text in ('', '-', '--'):
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0

    @staticmethod
    def _normalize_ticker(raw: Any) -> str:
        text = str(raw or '').strip().upper()
        if not text:
            return ''
        if text.startswith('A') and len(text) >= 7 and text[1:7].isdigit():
            return text[1:7]
        m = re.search(r'(\d{6})$', text)
        if m:
            return m.group(1)
        m = re.search(r'(\d{6})', text)
        if m:
            return m.group(1)
        return text

    @staticmethod
    def _extract_rows(payload: Any) -> List[Dict]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]

        if not isinstance(payload, dict):
            return []

        preferred_keys = ('output', 'OutBlock_1', 'OutBlock1', 'data', 'result', 'items')
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]

        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                for nested in value.values():
                    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                        return nested
        return []

    def _request_rows(self, urls: List[str], market: str, target_date: str) -> List[Dict]:
        if not self.api_key:
            return []

        urls_preferred = [u for u in urls if u.startswith("https://")] or urls

        profiles: List[Tuple[str, str, str]] = []
        cached = self._working_profiles.get(market)
        if cached is not None:
            profiles.append(cached)
        profiles.extend(p for p in self.REQUEST_PROFILES if p != cached)

        auth_error_count = 0
        had_success_status = False

        for url in urls_preferred:
            for mode, header_key, date_key in profiles:
                headers = {header_key: self.api_key}
                payload = {date_key: target_date}

                resp = None
                for _ in range(max(1, Config.KRX_OPENAPI_RETRY)):
                    try:
                        if mode == "GET":
                            resp = self.session.get(
                                url,
                                headers=headers,
                                params=payload,
                                timeout=self.timeout,
                            )
                        elif mode == "POST_DATA":
                            resp = self.session.post(
                                url,
                                headers=headers,
                                data=payload,
                                timeout=self.timeout,
                            )
                        else:
                            resp = self.session.post(
                                url,
                                headers=headers,
                                json=payload,
                                timeout=self.timeout,
                            )
                        break
                    except requests.RequestException as e:
                        logger.debug(
                            f"KRX OpenAPI 요청 실패 ({market}, {mode}, {header_key}, {date_key}): {e}"
                        )

                if resp is None:
                    continue

                if resp.status_code in (401, 403):
                    auth_error_count += 1
                    continue
                if resp.status_code != 200:
                    continue

                had_success_status = True
                try:
                    rows = self._extract_rows(resp.json())
                except ValueError:
                    rows = []

                if rows:
                    self._working_profiles[market] = (mode, header_key, date_key)
                    return rows

        if auth_error_count and not had_success_status:
            logger.warning(f"{market} KRX OpenAPI 인증 실패(401/403) - AUTH_KEY 확인 필요")
        return []

    def fetch_market_ohlcv_df(self, target_date: str, market: str) -> Optional[pd.DataFrame]:
        urls = self.ENDPOINTS.get(market)
        if not urls:
            return None

        rows = self._request_rows(urls=urls, market=market, target_date=target_date)
        if not rows:
            logger.warning(f"{market} KRX OpenAPI 응답 rows 없음 ({target_date})")
            return None

        parsed: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            ticker = self._normalize_ticker(
                row.get('ISU_CD') or row.get('ISU_SRT_CD') or row.get('isuCd') or row.get('isuSrtCd')
            )
            if not ticker:
                continue

            close = self._to_int(row.get('TDD_CLSPRC') or row.get('tddClsprc'))
            volume = self._to_int(row.get('ACC_TRDVOL') or row.get('accTrdvol'))
            if close <= 0 or volume <= 0:
                continue

            name = str(row.get('ISU_NM') or row.get('isuNm') or '').strip() or ticker
            parsed[ticker] = {
                '종가': close,
                '거래량': volume,
                '종목명': name,
            }

        if not parsed:
            return None

        df = pd.DataFrame.from_dict(parsed, orient='index')
        df.index.name = '티커'
        return df


class StockDataFetcher:
    """주식 데이터 수집 클래스"""

    def __init__(self):
        # 날짜별 전종목 종가 캐시 {date: {ticker: price}} - 백테스트 follow-up 중복 API 방지
        self._price_cache: Dict[str, Dict[str, int]] = {}
        self.krx_openapi = KrxOpenApiFetcher(
            api_key=Config.KRX_API_KEY,
            timeout=Config.KRX_API_TIMEOUT
        )

    def _fetch_ohlcv_df(self, date: str, market: str):
        """단일 시장 OHLCV DataFrame 반환 (공통 호출 래퍼)"""
        if Config.KRX_API_KEY:
            df = self.krx_openapi.fetch_market_ohlcv_df(date, market)
            if df is not None and not df.empty:
                return df
            if not Config.USE_PYKRX_FALLBACK:
                return None
            logger.warning(f"{market} KRX OpenAPI 데이터 없음 ({date}) - pykrx fallback 시도")

        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(date, market=market)
            return df if df is not None and not df.empty else None
        except Exception as e:
            logger.error(f"{market} OHLCV 수집 실패 ({date}): {e}")
            return None

    def get_all_ohlcv(self, target_date: str) -> Dict[str, Dict]:
        """
        KOSPI+KOSDAQ 전종목 OHLCV bulk 수집 (API 2회 호출)
        Returns: {ticker: {current_price, volume, ...}}
        """
        result = {}
        for market in ['KOSPI', 'KOSDAQ']:
            df = self._fetch_ohlcv_df(target_date, market)
            if df is not None:
                for ticker in df.index:
                    row = df.loc[ticker]
                    close = int(row.get('종가', 0))
                    volume = int(row.get('거래량', 0))
                    name = str(row.get('종목명', ticker))
                    if close > 0 and volume > 0:
                        result[ticker] = {
                            'ticker': ticker,
                            'name': name,
                            'current_price': close,
                            'volume': volume,
                            'prev_close': 0,
                            'prev_volume': 0,
                            'foreign_cumulative': 0
                        }
                logger.info(f"{market} bulk 수집: {len(df)}종목")
        return result

    def get_prev_ohlcv(self, target_date: str) -> Dict[str, Dict]:
        """
        전일 OHLCV bulk 수집 (최대 7일 전까지 탐색해 가장 최근 거래일 사용)
        Returns: {ticker: {prev_close, prev_volume}}
        """
        for delta in range(1, 8):
            prev_date = (datetime.strptime(target_date, '%Y%m%d') - timedelta(days=delta)).strftime('%Y%m%d')
            result = {}
            found = False
            for market in ['KOSPI', 'KOSDAQ']:
                df = self._fetch_ohlcv_df(prev_date, market)
                if df is not None:
                    for ticker in df.index:
                        row = df.loc[ticker]
                        result[ticker] = {
                            'prev_close': int(row.get('종가', 0)),
                            'prev_volume': int(row.get('거래량', 0))
                        }
                    found = True
            if found:
                logger.info(f"전일 데이터 기준일: {prev_date} ({len(result)}종목)")
                return result
        return {}

    def get_stock_data_by_date(self, ticker: str, target_date: str) -> Optional[Dict]:
        """
        단일 종목 종가 조회 - 날짜별 bulk 캐시 사용 (BacktestTracker용)
        같은 날짜는 최초 1회만 API 호출 후 재사용
        """
        if target_date not in self._price_cache:
            day_prices: Dict[str, int] = {}
            for market in ['KOSPI', 'KOSDAQ']:
                df = self._fetch_ohlcv_df(target_date, market)
                if df is not None:
                    for t in df.index:
                        price = int(df.loc[t].get('종가', 0))
                        if price > 0:
                            day_prices[t] = price
            self._price_cache[target_date] = day_prices
            logger.debug(f"가격 캐시 저장: {target_date} ({len(day_prices)}종목)")

        price = self._price_cache[target_date].get(ticker, 0)
        if price > 0:
            return {'ticker': ticker, 'current_price': price}
        return None

    def get_foreign_buy_data_by_date(self, ticker: str, target_date: str) -> int:
        """
        특정 날짜 기준 외국인 10영업일 누적 순매수 (개별 종목 - 필터 통과 종목에만 호출)
        """
        try:
            end_dt = datetime.strptime(target_date, '%Y%m%d')
            start_date = pd.bdate_range(end=end_dt, periods=10)[0].strftime('%Y%m%d')

            investor_data = pykrx_stock.get_market_trading_volume_by_investor(
                start_date, target_date, ticker
            )

            if investor_data is not None and not investor_data.empty:
                idx_name = None
                for candidate in ('외국인', '외국인합계'):
                    if candidate in investor_data.index:
                        idx_name = candidate
                        break

                if idx_name is not None:
                    if isinstance(investor_data.columns, pd.MultiIndex):
                        target_col = next(
                            (c for c in investor_data.columns if str(c[-1]).strip() == '순매수'),
                            None
                        )
                    else:
                        target_col = '순매수' if '순매수' in investor_data.columns else None

                    if target_col is not None:
                        foreign_net_buy = int(investor_data.loc[idx_name, target_col])
                        logger.debug(f"[{ticker}] 외국인 10영업일 순매수(pykrx): {foreign_net_buy}")
                        return foreign_net_buy

        except Exception as e:
            logger.debug(f"외국인 데이터 수집 실패(pykrx) {ticker}: {e}")

        fallback = self._get_foreign_buy_from_naver(ticker=ticker, target_date=target_date, days=10)
        if fallback is not None:
            logger.debug(f"[{ticker}] 외국인 10영업일 순매수(naver): {fallback}")
            return fallback
        return 0

    @staticmethod
    def _to_int_value(value: Any) -> int:
        text = str(value).strip().replace(',', '').replace('+', '')
        if text in ('', 'nan', 'None', '-'):
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0

    def _get_foreign_buy_from_naver(self, ticker: str, target_date: str, days: int = 10) -> Optional[int]:
        """네이버 금융 개별종목 외국인 순매매량 페이지를 사용한 폴백."""
        try:
            target_dt = datetime.strptime(target_date, '%Y%m%d').date()
        except ValueError:
            return None

        rows: List[Tuple[datetime.date, int]] = []
        headers = {"User-Agent": "Mozilla/5.0"}

        for page in range(1, 6):
            url = f"https://finance.naver.com/item/frgn.naver?code={ticker}&page={page}"
            try:
                resp = requests.get(url, headers=headers, timeout=8)
                resp.raise_for_status()
                tables = pd.read_html(StringIO(resp.text))
            except Exception:
                continue

            # 테이블 인덱스가 종목 규모에 따라 다르므로 컬럼 내용으로 탐색
            target_table = None
            for t in tables:
                cols = t.columns
                if isinstance(cols, pd.MultiIndex):
                    flat = [" ".join([str(x).strip() for x in c if str(x).strip() and str(x).strip().lower() != 'nan']) for c in cols]
                else:
                    flat = [str(c).strip() for c in cols]
                if any('날짜' in c for c in flat) and any('외국인' in c and '순매매량' in c for c in flat):
                    t.columns = flat
                    target_table = t
                    break

            if target_table is None:
                continue

            table = target_table
            date_col = next((c for c in table.columns if '날짜' in c), None)
            foreign_col = next((c for c in table.columns if '외국인' in c and '순매매량' in c), None)
            if not date_col or not foreign_col:
                continue

            for _, row in table.iterrows():
                raw_date = str(row.get(date_col, '')).strip()
                if not raw_date or raw_date.lower() == 'nan':
                    continue

                try:
                    row_dt = datetime.strptime(raw_date, '%Y.%m.%d').date()
                except ValueError:
                    continue

                if row_dt > target_dt:
                    continue

                rows.append((row_dt, self._to_int_value(row.get(foreign_col, 0))))

            if len(rows) >= days:
                break

        if not rows:
            return None

        by_date: Dict[datetime.date, int] = {}
        for dt, value in rows:
            if dt not in by_date:
                by_date[dt] = value

        selected = sorted(by_date.items(), key=lambda x: x[0], reverse=True)[:days]
        if not selected:
            return None

        return int(sum(value for _, value in selected))



# ============================================================================
# 필터링 (FILTER)
# ============================================================================

class StockFilter:
    """주식 필터링 클래스"""
    
    @staticmethod
    def check_conditions(stock_data: Dict, require_foreign: bool = True) -> Tuple[bool, Dict]:
        """
        3가지 조건 검사
        1. 거래량 전날 대비 2.5배 이상
        2. 주가 4% 이상 하락
        3. 외국인 순매수 누적 (양수)
        
        Return: (조건충족여부, 상세정보)
        """
        
        conditions_met = {
            'volume': False,
            'price_drop': False,
            'foreign_buy': False
        }
        
        scores = {
            'volume': 0,
            'price_drop': 0,
            'foreign_buy': 0
        }
        
        # 계산된 값들 저장
        volume_ratio = 0
        price_change_pct = 0
        foreign_cumulative = 0
        
        try:
            # 조건 1: 거래량
            if stock_data.get('volume', 0) > 0 and stock_data.get('prev_volume', 0) > 0:
                volume_ratio = stock_data['volume'] / stock_data['prev_volume']
                if volume_ratio >= Config.VOLUME_RATIO:
                    conditions_met['volume'] = True
                    scores['volume'] = min(100, int(volume_ratio * 20))  # 스코어
            
            # 조건 2: 주가 하락
            if stock_data.get('current_price', 0) > 0 and stock_data.get('prev_close', 0) > 0:
                price_change_pct = ((stock_data['current_price'] - stock_data['prev_close']) 
                                   / stock_data['prev_close'] * 100)
                if price_change_pct <= Config.PRICE_DROP_PCT:
                    conditions_met['price_drop'] = True
                    scores['price_drop'] = min(100, int(abs(price_change_pct) * 10))
            
            # 조건 3: 외국인 순매수
            foreign_cumulative = stock_data.get('foreign_cumulative', 0)
            if require_foreign and foreign_cumulative > 0:
                conditions_met['foreign_buy'] = True
                scores['foreign_buy'] = min(100, int(foreign_cumulative / 1000))
            elif not require_foreign:
                conditions_met['foreign_buy'] = True
            
            # 3가지 모두 만족 여부
            all_conditions_met = all(conditions_met.values())
            active_scores = [scores['volume'], scores['price_drop']]
            if require_foreign:
                active_scores.append(scores['foreign_buy'])
            total_score = sum(active_scores) // len(active_scores) if active_scores else 0
            
            return all_conditions_met, {
                'total_score': total_score,
                'volume_ratio': round(volume_ratio, 2),
                'price_change_pct': round(price_change_pct, 2),
                'foreign_cumulative': int(foreign_cumulative),
                'foreign_required': require_foreign,
            }
            
        except Exception as e:
            logger.error(f"필터링 오류: {e}")
            return False, {'error': str(e)}


# ============================================================================
# 분석 (ANALYZER)
# ============================================================================

class StockAnalyzer:
    """주식 분석 클래스"""
    
    @staticmethod
    def format_stock_message(ticker: str, stock_data: Dict, name: str, filter_result: Dict) -> str:
        """종목 메시지 포맷팅"""
        try:
            volume_ratio = filter_result.get('volume_ratio', 0)
            price_drop = filter_result.get('price_change_pct', 0)
            foreign_buy = filter_result.get('foreign_cumulative', 0)
            foreign_required = filter_result.get('foreign_required', True)

            if foreign_required:
                foreign_line = f"🌍 10영업일 외국인 순매수: {foreign_buy:,}주"
            else:
                foreign_line = "🌍 10영업일 외국인 순매수: 조회 스킵"

            return (
                f"✅ {name} ({ticker})\n"
                f"현재가: {stock_data.get('current_price', 0):,}원\n"
                f"📊 거래량: {volume_ratio}배 증가\n"
                f"📉 주가: {price_drop:.2f}% 하락\n"
                f"{foreign_line}\n"
            )
        except Exception as e:
            logger.error(f"메시지 포맷 오류: {e}")
            return f"오류: {ticker}"


# ============================================================================
# 텔레그램 발송 (TELEGRAM SENDER)
# ============================================================================

class TelegramSender:
    """텔레그램 메시지 발송 클래스"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send_message(self, message: str) -> bool:
        """메시지 발송"""
        try:
            if self.bot_token == "YOUR_BOT_TOKEN":
                import sys
                logger.warning("텔레그램 토큰이 설정되지 않았습니다.")
                encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
                print(f"\n[테스트 메시지]\n{safe_message}\n")
                return False
            
            url = f"{self.api_url}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=data, timeout=Config.REQUEST_TIMEOUT)

            if response.status_code == 200:
                logger.debug("텔레그램 메시지 발송 성공")
                return True
            else:
                logger.error(f"텔레그램 발송 실패: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"텔레그램 발송 오류: {e}")
            return False
    
    def send_summary(self, results: List[Dict], total_scanned: int = 0, scan_date: str = None) -> None:
        """스캔 결과를 기업별로 개별 메시지 발송"""
        try:
            # 날짜 포맷: 20260213 → 2026/02/13
            if scan_date:
                date_str = f"{scan_date[:4]}/{scan_date[4:6]}/{scan_date[6:]}"
            else:
                date_str = datetime.now().strftime('%Y/%m/%d')

            if not results:
                message = f"📊 {date_str} 스캔 결과\n{total_scanned}종목 검사 → 신호 없음"
                logger.info(f"[send_summary] 신호 없음 메시지 발송")
                self.send_message(message)
            else:
                # 1. 먼저 요약 메시지 발송
                summary_msg = f"📊 {date_str} 스캔 결과\n{total_scanned}종목 검사 → {len(results)}개 통과\n\n아래 종목 상세 정보를 확인하세요."
                self.send_message(summary_msg)
                logger.info(f"[send_summary] 요약 메시지 발송 완료 ({total_scanned}종목 중 {len(results)}개)")
                time.sleep(0.5)

                # 2. 각 종목별로 개별 메시지 발송
                logger.info(f"[send_summary] {len(results)}개 종목 각각 메시지 발송 시작")
                for idx, result in enumerate(results, 1):
                    self.send_message(result.get('message', ''))
                    logger.debug(f"[send_summary] [{idx}/{len(results)}] {result.get('name')} 메시지 발송")
                    time.sleep(0.5)  # API 과부하 방지

        except Exception as e:
            logger.error(f"요약 메시지 발송 오류: {e}")


# ============================================================================
# 백테스트 추적 (BACKTEST TRACKER)
# ============================================================================

class BacktestTracker:
    """
    백테스트 추적 클래스
    - 스캔 통과 종목과 기준 종가를 JSON 파일에 저장
    - 매일 실행 시 3/5/10/15/30일 후 수익률을 텔레그램으로 발송
    """

    FOLLOW_UP_DAYS = [3, 5, 10, 15, 30]
    DATA_FILE = 'backtest_data.json'

    def __init__(self, fetcher: 'StockDataFetcher', telegram: 'TelegramSender'):
        self.fetcher = fetcher
        self.telegram = telegram
        self.data = self._load_data()

    def _load_data(self) -> Dict:
        """저장된 백테스트 데이터 로드"""
        if os.path.exists(self.DATA_FILE):
            try:
                with open(self.DATA_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"백테스트 데이터 로드 실패: {e}")
        return {'scans': []}

    def _save_data(self) -> None:
        """백테스트 데이터 저장"""
        try:
            with open(self.DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"백테스트 데이터 저장 실패: {e}")

    def save_scan_results(self, scan_date: str, results: List[Dict]) -> None:
        """스캔 통과 종목 저장 (이미 같은 날짜가 있으면 업데이트)"""
        if not results:
            logger.info(f"백테스트: {scan_date} 통과 종목 없음, 저장 생략")
            return

        stocks = [
            {'ticker': r['ticker'], 'name': r['name'], 'base_price': r.get('base_price', 0)}
            for r in results
        ]

        for scan in self.data['scans']:
            if scan['scan_date'] == scan_date:
                scan['stocks'] = stocks
                self._save_data()
                logger.info(f"백테스트: {scan_date} 기존 데이터 업데이트 ({len(stocks)}개 종목)")
                return

        self.data['scans'].append({
            'scan_date': scan_date,
            'stocks': stocks,
            'sent_followups': []
        })
        self._save_data()
        logger.info(f"백테스트: {scan_date} 신규 저장 ({len(stocks)}개 종목)")

    def _get_price_on_or_after(self, ticker: str, target_date: str) -> Tuple[Optional[int], Optional[str]]:
        """target_date 이후 가장 가까운 거래일의 종가 반환 (최대 5영업일 탐색)"""
        today = datetime.now().date()
        for offset in range(6):
            check_dt = datetime.strptime(target_date, '%Y%m%d') + timedelta(days=offset)
            # 아직 도래하지 않은 미래 날짜는 조회하지 않는다.
            if check_dt.date() > today:
                break
            check_date = check_dt.strftime('%Y%m%d')
            data = self.fetcher.get_stock_data_by_date(ticker, check_date)
            if data and data.get('current_price', 0) > 0:
                return data['current_price'], check_date
        return None, None

    def check_and_send_followups(self) -> None:
        """오늘 기준으로 due된 follow-up 메시지 발송"""
        today = datetime.now().date()
        changed = False

        for scan in self.data['scans']:
            if not scan.get('stocks'):
                continue

            scan_date_dt = datetime.strptime(scan['scan_date'], '%Y%m%d').date()
            # 영업일 기준 경과일 계산 (주말 제외)
            bdays_elapsed = len(pd.bdate_range(scan_date_dt, today)) - 1

            for follow_days in self.FOLLOW_UP_DAYS:
                already_sent = follow_days in scan.get('sent_followups', [])
                if bdays_elapsed >= follow_days and not already_sent:
                    sent = self._send_followup_message(scan, follow_days)
                    if sent:
                        scan.setdefault('sent_followups', []).append(follow_days)
                        changed = True
                    else:
                        logger.warning(
                            f"백테스트 {follow_days}일 후 전송 보류 (기준일: {scan['scan_date']}) - 다음 실행에서 재시도"
                        )

        if changed:
            self._save_data()

    def _send_followup_message(self, scan: Dict, follow_days: int) -> bool:
        """follow-up 수익률 메시지 생성 및 발송 (전송 성공 시 True)"""
        scan_date = scan['scan_date']
        # N 영업일 후 날짜 계산 (주말 제외)
        scan_dt = datetime.strptime(scan_date, '%Y%m%d')
        target_date = pd.bdate_range(start=scan_dt, periods=follow_days + 1)[-1].strftime('%Y%m%d')

        sd = f"{scan_date[:4]}/{scan_date[4:6]}/{scan_date[6:]}"
        td = f"{target_date[:4]}/{target_date[4:6]}/{target_date[6:]}"

        lines = [
            f"📊 백테스트 결과 ({follow_days}영업일 후)",
            f"📅 기준일: {sd} → {td}",
            ""
        ]

        gains = []
        resolved_prices = 0
        for stock in scan['stocks']:
            ticker = stock['ticker']
            name = stock['name']
            base_price = stock.get('base_price', 0)

            if base_price <= 0:
                lines.append(f"❓ {name} ({ticker}): 기준가 없음")
                continue

            target_price, actual_date = self._get_price_on_or_after(ticker, target_date)

            if target_price:
                resolved_prices += 1
                change_pct = (target_price - base_price) / base_price * 100
                emoji = "📈" if change_pct >= 0 else "📉"
                gains.append(change_pct)
                note = f" ({actual_date[4:6]}/{actual_date[6:]} 기준)" if actual_date != target_date else ""
                lines.append(f"{emoji} {name}: {base_price:,}원 → {target_price:,}원 ({change_pct:+.2f}%){note}")
            else:
                lines.append(f"❓ {name} ({ticker}): 데이터 없음")

        # 전 종목 가격 조회 실패 시 알림/기록을 생략하고 다음 실행에서 재시도한다.
        if resolved_prices == 0:
            logger.warning(f"백테스트 {follow_days}일 후 유효 가격 0건 (기준일: {scan_date})")
            return False

        if gains:
            avg = sum(gains) / len(gains)
            win_rate = sum(1 for g in gains if g > 0) / len(gains) * 100
            lines.append("")
            lines.append(f"📊 평균 수익률: {avg:+.2f}%")
            lines.append(f"✅ 승률: {win_rate:.0f}% ({sum(1 for g in gains if g > 0)}/{len(gains)})")

        self.telegram.send_message("\n".join(lines))
        logger.info(f"백테스트 {follow_days}일 후 메시지 발송 완료 (기준일: {scan_date})")
        return True


# ============================================================================
# 메인 실행 (MAIN EXECUTOR)
# ============================================================================

class StockScanner:
    """주식 스캐너 메인 클래스"""
    
    def __init__(self):
        self.fetcher = StockDataFetcher()
        self.filter = StockFilter()
        self.analyzer = StockAnalyzer()
        self.telegram = TelegramSender(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
        self.backtest = BacktestTracker(self.fetcher, self.telegram)
        self.last_scan_date = None
        self.foreign_buy_enabled = False
        self.foreign_auth_message = ""

    @staticmethod
    def _previous_business_day(date_str: str) -> str:
        dt = datetime.strptime(date_str, '%Y%m%d').date() - timedelta(days=1)
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
        return dt.strftime('%Y%m%d')

    def _prepare_foreign_session(self) -> None:
        """매 실행마다 KRX 로그인 후 pykrx에 세션을 주입"""
        user_id = Config.KRX_USER_ID.strip()
        password = Config.KRX_PASSWORD.strip()

        if not user_id or not password:
            self.foreign_buy_enabled = True
            self.foreign_auth_message = "KRX 로그인 미설정: 외국인 순매수는 네이버 데이터로 조회합니다."
            logger.info(self.foreign_auth_message)
            return

        auth = KrxSessionAuth(user_id=user_id, password=password, timeout=Config.KRX_API_TIMEOUT)
        if auth.login() and auth.inject_to_pykrx():
            self.foreign_buy_enabled = True
            self.foreign_auth_message = ""
        else:
            self.foreign_buy_enabled = True
            self.foreign_auth_message = "KRX 로그인 실패: 외국인 순매수는 네이버 데이터로 대체합니다."
            logger.warning(self.foreign_auth_message)
    
    def scan(self, scan_date: str = None) -> List[Dict]:
        """전종목 스캔 실행 (bulk API 방식 - API 4~6회 호출로 전종목 처리)"""
        try:
            logger.info("=" * 60)
            logger.info(f"스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("=" * 60)

            requested_date = scan_date or datetime.now().strftime('%Y%m%d')
            if scan_date is None:
                data_date = self._previous_business_day(requested_date)
                logger.info(f"요청 날짜: {requested_date} / 실제 조회 날짜(전 영업일): {data_date}")
            else:
                data_date = requested_date

            logger.info(f"기준 날짜: {data_date}")

            # 1. 전종목 당일 OHLCV bulk 수집 (API 2회)
            logger.info("기준일 전종목 OHLCV 수집 중...")
            today_data = self.fetcher.get_all_ohlcv(data_date)
            if not today_data:
                logger.warning("기준일 데이터 없음 - 스캔 종료")
                self.last_scan_date = data_date
                return []

            # 2. 전종목 전일 OHLCV bulk 수집 (API 2회)
            logger.info("전일 전종목 OHLCV 수집 중...")
            prev_data = self.fetcher.get_prev_ohlcv(data_date)

            logger.info(f"당일: {len(today_data)}종목 / 전일: {len(prev_data)}종목")

            # 3. 전일 데이터 병합 + 거래량/가격 1차 필터 (메모리 처리, API 없음)
            candidates = []
            for ticker, stock_data in today_data.items():
                if ticker in prev_data:
                    stock_data['prev_close'] = prev_data[ticker]['prev_close']
                    stock_data['prev_volume'] = prev_data[ticker]['prev_volume']

                vol = stock_data.get('volume', 0)
                prev_vol = stock_data.get('prev_volume', 0)
                cur = stock_data.get('current_price', 0)
                prev_c = stock_data.get('prev_close', 0)

                if prev_vol <= 0 or prev_c <= 0:
                    continue

                volume_ratio = vol / prev_vol
                price_change = (cur - prev_c) / prev_c * 100

                if volume_ratio >= Config.VOLUME_RATIO and price_change <= Config.PRICE_DROP_PCT:
                    candidates.append((ticker, stock_data, volume_ratio, price_change))

            logger.info(f"1+2차 필터 통과 (거래량+가격): {len(candidates)}종목 → 외국인 데이터 조회")

            # 4. 후보 종목만 외국인 데이터 개별 조회 + 3차 필터
            results = []
            for ticker, stock_data, volume_ratio, price_change in candidates:
                name = stock_data.get('name', ticker)

                if self.foreign_buy_enabled:
                    cumulative = self.fetcher.get_foreign_buy_data_by_date(ticker, data_date)
                else:
                    cumulative = 0
                stock_data['foreign_cumulative'] = cumulative

                conditions_met, filter_result = self.filter.check_conditions(
                    stock_data,
                    require_foreign=self.foreign_buy_enabled
                )

                if conditions_met:
                    message = self.analyzer.format_stock_message(ticker, stock_data, name, filter_result)
                    results.append({
                        'ticker': ticker,
                        'name': name,
                        'message': message,
                        'score': filter_result.get('total_score', 0),
                        'base_price': stock_data.get('current_price', 0)
                    })
                    if self.foreign_buy_enabled:
                        logger.info(f"✓ {name} ({ticker}) 거래량{volume_ratio:.1f}배 / {price_change:.1f}% / 외국인 {cumulative:+,}주")
                    else:
                        logger.info(f"✓ {name} ({ticker}) 거래량{volume_ratio:.1f}배 / {price_change:.1f}% / 외국인 조건 스킵")

            results.sort(key=lambda x: x['score'], reverse=True)
            self.total_scanned = len(today_data)
            logger.info(f"스캔 완료: {len(today_data)}종목 검사 → {len(results)}종목 통과")

            self.last_scan_date = data_date
            return results
            
        except Exception as e:
            logger.error(f"스캔 오류: {e}", exc_info=True)
            return []
    
    def execute(self, scan_date: str = None) -> None:
        """전체 실행"""
        try:
            # 1. 과거 백테스트 follow-up 메시지 먼저 발송
            logger.info("[execute] 백테스트 follow-up 확인 중...")
            self.backtest.check_and_send_followups()

            # 2. 외국인 순매수 조회용 세션 준비
            self._prepare_foreign_session()
            if not self.foreign_buy_enabled:
                self.telegram.send_message(
                    f"⚠️ {self.foreign_auth_message}\n"
                    "오늘 스캔은 거래량/주가 하락 조건만으로 진행합니다."
                )

            # 3. 스캔 실행
            results = self.scan(scan_date=scan_date)
            total_stocks = getattr(self, 'total_scanned', 0)

            # 4. 결과 텔레그램 발송 (send_summary 내부에서 결과 유무 처리)
            self.telegram.send_summary(results, total_stocks, scan_date=self.last_scan_date)

            # 5. 백테스트 데이터 저장 (통과 종목이 있을 때만)
            if results and self.last_scan_date:
                self.backtest.save_scan_results(self.last_scan_date, results)

            logger.info("=" * 60)
            logger.info("실행 완료")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"실행 오류: {e}", exc_info=True)
            self.telegram.send_message(f"❌ 스캔 오류: {str(e)}")


# ============================================================================
# 스케줄러 (SCHEDULER)
# ============================================================================

class AutoStockScheduler:
    """자동 실행 스케줄러"""
    
    def __init__(self):
        self.scheduler = BlockingScheduler()
        self.scanner = StockScanner()
    
    def setup(self) -> None:
        """스케줄 설정"""
        # 매일 오후 6시 (18:00) 실행
        self.scheduler.add_job(
            self.run_scan,
            CronTrigger(hour=18, minute=0),
            id='daily_scan',
            name='Daily Stock Scan',
            misfire_grace_time=60
        )
        
        logger.info(f"스케줄 설정 완료: 매일 {Config.RUN_TIME} 실행")
    
    def run_scan(self) -> None:
        """스캔 실행"""
        logger.info("스케줄된 스캔 시작...")
        try:
            self.scanner.execute()
        except Exception as e:
            logger.error(f"스케줄 실행 오류: {e}", exc_info=True)
    
    def start(self) -> None:
        """스케줄러 시작"""
        try:
            self.setup()
            logger.info("\n⏰ 스케줄러 시작됨. Ctrl+C로 종료")
            self.scheduler.start()
        except Exception as e:
            logger.error(f"스케줄러 시작 실패: {e}")
    
    def run_once(self) -> None:
        """한 번만 실행 (테스트용)"""
        logger.info("\n🔍 수동 스캔 시작 (테스트 모드)")
        logger.info("[run_once] execute() 호출 - 메시지 1번 발송")
        self.scanner.execute()


# ============================================================================
# 진입점 (ENTRY POINT)
# ============================================================================

def main():
    """메인 함수"""
    import sys
    
    logger.info("\n" + "=" * 60)
    logger.info("📊 주식 자동화 스캐너 시작")
    logger.info("=" * 60 + "\n")
    
    # 토큰 확인
    if Config.TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        logger.warning("⚠️ 텔레그램 토큰이 설정되지 않았습니다.")
        logger.warning("   Config.TELEGRAM_BOT_TOKEN 을 수정하세요.\n")
    
    # 인자 확인
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # 테스트 모드: 한 번만 실행
        logger.info("🧪 테스트 모드로 실행\n")
        AutoStockScheduler().run_once()
    elif len(sys.argv) > 1 and sys.argv[1].startswith('--date='):
        # 특정 날짜 모드: 해당 날짜 데이터로 스캔
        scan_date = sys.argv[1].replace('--date=', '')
        logger.info(f"📅 특정 날짜 모드: {scan_date}\n")
        StockScanner().execute(scan_date=scan_date)
    else:
        # 스케줄 모드: 매일 오후 6시 자동 실행
        AutoStockScheduler().start()


if __name__ == "__main__":
    main()
