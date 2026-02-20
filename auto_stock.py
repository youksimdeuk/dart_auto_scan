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
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import time
from pykrx import stock as pykrx_stock

# pykrx 내부 HTTP 요청 전역 타임아웃 (네트워크 hanging 방지)
socket.setdefaulttimeout(15)

# 로깅 설정
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# 설정 (CONFIG)
# ============================================================================

class Config:
    """설정 클래스"""
    # 텔레그램 토큰, 채팅 ID (환경변수에서 읽기)
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', "8587133354:AAFo1AviKo7ENcW55F8mnDhFniS-PsAmHgY")
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', "1249787976")
    
    # 필터 조건
    VOLUME_RATIO = 2.5                      # 거래량 배수
    PRICE_DROP_PCT = -4.0                   # 주가 하락률 (%)
    FOREIGN_BUY_SIGNAL = True               # 외국인순매수 신호
    
    # API 설정
    REQUEST_TIMEOUT = 10
    REQUEST_RETRY = 3
    
    # 실행 설정
    RUN_TIME = "18:00"                      # 오후 6시 (한국 기준)
    
    # 테스트 모드 (True 시 더미 데이터 사용)
    TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'


# ============================================================================
# 데이터 수집 (DATA FETCHER)
# ============================================================================

class StockDataFetcher:
    """주식 데이터 수집 클래스"""

    def __init__(self):
        # 날짜별 전종목 종가 캐시 {date: {ticker: price}} - 백테스트 follow-up 중복 API 방지
        self._price_cache: Dict[str, Dict[str, int]] = {}

    def get_all_ohlcv(self, target_date: str) -> Dict[str, Dict]:
        """
        KOSPI+KOSDAQ 전종목 OHLCV를 bulk API로 한번에 수집 (API 2회 호출)
        Returns: {ticker: {current_price, volume}}
        """
        result = {}
        for market in ['KOSPI', 'KOSDAQ']:
            try:
                df = pykrx_stock.get_market_ohlcv_by_ticker(target_date, market=market)
                if df is not None and not df.empty:
                    for ticker in df.index:
                        row = df.loc[ticker]
                        close = int(row.get('종가', 0))
                        volume = int(row.get('거래량', 0))
                        if close > 0 and volume > 0:
                            result[ticker] = {
                                'ticker': ticker,
                                'current_price': close,
                                'volume': volume,
                                'prev_close': 0,
                                'prev_volume': 0,
                                'foreign_cumulative': 0
                            }
                    logger.info(f"{market} bulk 수집: {len(df)}종목")
            except Exception as e:
                logger.error(f"{market} OHLCV bulk 수집 실패: {e}")
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
                try:
                    df = pykrx_stock.get_market_ohlcv_by_ticker(prev_date, market=market)
                    if df is not None and not df.empty:
                        for ticker in df.index:
                            row = df.loc[ticker]
                            result[ticker] = {
                                'prev_close': int(row.get('종가', 0)),
                                'prev_volume': int(row.get('거래량', 0))
                            }
                        found = True
                except Exception as e:
                    logger.error(f"{market} 전일 OHLCV 수집 실패 ({prev_date}): {e}")
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
                try:
                    df = pykrx_stock.get_market_ohlcv_by_ticker(target_date, market=market)
                    if df is not None and not df.empty:
                        for t in df.index:
                            price = int(df.loc[t].get('종가', 0))
                            if price > 0:
                                day_prices[t] = price
                except Exception as e:
                    logger.debug(f"캐시 수집 실패 ({target_date}, {market}): {e}")
            self._price_cache[target_date] = day_prices
            logger.debug(f"가격 캐시 저장: {target_date} ({len(day_prices)}종목)")

        price = self._price_cache[target_date].get(ticker, 0)
        if price > 0:
            return {'ticker': ticker, 'current_price': price}
        return None

    def get_foreign_buy_data_by_date(self, ticker: str, target_date: str) -> Tuple[float, float]:
        """
        특정 날짜 기준 외국인 10일 누적 순매수 (개별 종목 - 필터 통과 종목에만 호출)
        """
        try:
            end_date = target_date
            start_date = (datetime.strptime(target_date, '%Y%m%d') - timedelta(days=13)).strftime('%Y%m%d')

            investor_data = pykrx_stock.get_market_trading_volume_by_investor(
                start_date, end_date, ticker
            )

            if investor_data is not None and not investor_data.empty:
                if '외국인' in investor_data.index:
                    foreign_net_buy = int(investor_data.loc['외국인', '순매수'])
                    logger.debug(f"[{ticker}] 외국인 10일 순매수: {foreign_net_buy}")
                    return foreign_net_buy, foreign_net_buy

        except Exception as e:
            logger.debug(f"외국인 데이터 수집 실패 {ticker}: {e}")
        return 0, 0



# ============================================================================
# 필터링 (FILTER)
# ============================================================================

class StockFilter:
    """주식 필터링 클래스"""
    
    @staticmethod
    def check_conditions(stock_data: Dict) -> Tuple[bool, Dict]:
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
            if foreign_cumulative > 0:
                conditions_met['foreign_buy'] = True
                scores['foreign_buy'] = min(100, int(foreign_cumulative / 1000))
            
            # 3가지 모두 만족 여부
            all_conditions_met = all(conditions_met.values())
            
            return all_conditions_met, {
                'conditions': conditions_met,
                'scores': scores,
                'total_score': sum(scores.values()) // 3,  # 평균 스코어
                'volume_ratio': round(volume_ratio, 2),
                'price_change_pct': round(price_change_pct, 2),
                'foreign_cumulative': int(foreign_cumulative)
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
    def format_stock_message(ticker: str, stock_data: Dict, name: str, filter_result: Dict = None) -> str:
        """종목 메시지 포맷팅 (상세 정보 포함)"""
        try:
            change_pct = ((stock_data.get('current_price', 0) - stock_data.get('prev_close', 0)) / stock_data.get('prev_close', 1) * 100)
            
            message = f"✅ {name} ({ticker})\n"
            message += f"현재가: {stock_data.get('current_price', 'N/A'):,}원\n"
            
            # 필터 결과에서 상세 정보 추출
            if filter_result:
                volume_ratio = filter_result.get('volume_ratio', 0)
                price_drop = filter_result.get('price_change_pct', 0)
                foreign_buy = filter_result.get('foreign_cumulative', 0)
                
                message += f"📊 거래량: {volume_ratio}배 증가\n"
                message += f"📉 주가: {price_drop:.2f}% 하락\n"
                message += f"🌍 10일 외국인 순매수: {foreign_buy:,}주\n"
            else:
                message += f"변화율: {change_pct:.2f}%\n"
            
            return message
            
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
                logger.warning("텔레그램 토큰이 설정되지 않았습니다.")
                print(f"\n[테스트 메시지]\n{message}\n")
                return False
            
            url = f"{self.api_url}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=data, timeout=10)
            
            if response.status_code == 200:
                logger.info("텔레그램 메시지 발송 성공")
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
        for offset in range(6):
            check_date = (datetime.strptime(target_date, '%Y%m%d') + timedelta(days=offset)).strftime('%Y%m%d')
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
            days_elapsed = (today - scan_date_dt).days

            for follow_days in self.FOLLOW_UP_DAYS:
                already_sent = follow_days in scan.get('sent_followups', [])
                if days_elapsed >= follow_days and not already_sent:
                    self._send_followup_message(scan, follow_days)
                    scan.setdefault('sent_followups', []).append(follow_days)
                    changed = True

        if changed:
            self._save_data()

    def _send_followup_message(self, scan: Dict, follow_days: int) -> None:
        """follow-up 수익률 메시지 생성 및 발송"""
        scan_date = scan['scan_date']
        target_date = (datetime.strptime(scan_date, '%Y%m%d') + timedelta(days=follow_days)).strftime('%Y%m%d')

        sd = f"{scan_date[:4]}/{scan_date[4:6]}/{scan_date[6:]}"
        td = f"{target_date[:4]}/{target_date[4:6]}/{target_date[6:]}"

        lines = [
            f"📊 백테스트 결과 ({follow_days}일 후)",
            f"📅 기준일: {sd} → {td}",
            ""
        ]

        gains = []
        for stock in scan['stocks']:
            ticker = stock['ticker']
            name = stock['name']
            base_price = stock.get('base_price', 0)

            if base_price <= 0:
                lines.append(f"❓ {name} ({ticker}): 기준가 없음")
                continue

            target_price, actual_date = self._get_price_on_or_after(ticker, target_date)

            if target_price:
                change_pct = (target_price - base_price) / base_price * 100
                emoji = "📈" if change_pct >= 0 else "📉"
                gains.append(change_pct)
                note = f" ({actual_date[4:6]}/{actual_date[6:]} 기준)" if actual_date != target_date else ""
                lines.append(f"{emoji} {name}: {base_price:,}원 → {target_price:,}원 ({change_pct:+.2f}%){note}")
            else:
                lines.append(f"❓ {name} ({ticker}): 데이터 없음")

        if gains:
            avg = sum(gains) / len(gains)
            win_rate = sum(1 for g in gains if g > 0) / len(gains) * 100
            lines.append("")
            lines.append(f"📊 평균 수익률: {avg:+.2f}%")
            lines.append(f"✅ 승률: {win_rate:.0f}% ({sum(1 for g in gains if g > 0)}/{len(gains)})")

        self.telegram.send_message("\n".join(lines))
        logger.info(f"백테스트 {follow_days}일 후 메시지 발송 완료 (기준일: {scan_date})")


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
    
    def scan(self, scan_date: str = None) -> List[Dict]:
        """전종목 스캔 실행 (bulk API 방식 - API 4~6회 호출로 전종목 처리)"""
        try:
            logger.info("=" * 60)
            logger.info(f"스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("=" * 60)

            if scan_date is None:
                scan_date = datetime.now().strftime('%Y%m%d')

            logger.info(f"기준 날짜: {scan_date}")

            # 1. 전종목 당일 OHLCV bulk 수집 (API 2회)
            logger.info("당일 전종목 OHLCV 수집 중...")
            today_data = self.fetcher.get_all_ohlcv(scan_date)
            if not today_data:
                logger.warning("당일 데이터 없음 - 스캔 종료")
                self.last_scan_date = scan_date
                return []

            # 2. 전종목 전일 OHLCV bulk 수집 (API 2회)
            logger.info("전일 전종목 OHLCV 수집 중...")
            prev_data = self.fetcher.get_prev_ohlcv(scan_date)

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
                try:
                    name = pykrx_stock.get_market_ticker_name(ticker) or ticker
                except Exception:
                    name = ticker

                _, cumulative = self.fetcher.get_foreign_buy_data_by_date(ticker, scan_date)
                stock_data['foreign_cumulative'] = cumulative

                conditions_met, filter_result = self.filter.check_conditions(stock_data)

                if conditions_met:
                    message = self.analyzer.format_stock_message(ticker, stock_data, name, filter_result)
                    results.append({
                        'ticker': ticker,
                        'name': name,
                        'message': message,
                        'score': filter_result.get('total_score', 0),
                        'base_price': stock_data.get('current_price', 0)
                    })
                    logger.info(f"✓ {name} ({ticker}) 거래량{volume_ratio:.1f}배 / {price_change:.1f}% / 외국인 {cumulative:+,}주")

            results.sort(key=lambda x: x['score'], reverse=True)
            self.total_scanned = len(today_data)
            logger.info(f"스캔 완료: {len(today_data)}종목 검사 → {len(results)}종목 통과")

            self.last_scan_date = scan_date
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

            # 2. 스캔 실행
            results = self.scan(scan_date=scan_date)
            total_stocks = getattr(self, 'total_scanned', 0)

            # 3. 결과 텔레그램 발송 (send_summary 내부에서 결과 유무 처리)
            self.telegram.send_summary(results, total_stocks, scan_date=self.last_scan_date)

            # 4. 백테스트 데이터 저장 (통과 종목이 있을 때만)
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
    
    scheduler = AutoStockScheduler()
    
    # 인자 확인
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # 테스트 모드: 한 번만 실행
        logger.info("🧪 테스트 모드로 실행\n")
        scheduler.run_once()
    elif len(sys.argv) > 1 and sys.argv[1].startswith('--date='):
        # 특정 날짜 모드: 해당 날짜 데이터로 스캔
        scan_date = sys.argv[1].replace('--date=', '')
        logger.info(f"📅 특정 날짜 모드: {scan_date}\n")
        StockScanner().execute(scan_date=scan_date)
    else:
        # 스케줄 모드: 매일 오후 4시 자동 실행
        scheduler.start()


if __name__ == "__main__":
    main()
