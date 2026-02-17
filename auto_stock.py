"""
주식 자동화 스캐너
- 전종목 모니터링 (KOSPI, KOSDAQ)
- 3가지 조건 필터링: 거래량 2.5배↑, 주가 4%↓, 외국인순매수+
- 매일 오후 4시 자동 실행
- 텔레그램 메시지 발송
- GitHub Actions에서 자동 실행
"""

import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import time
from bs4 import BeautifulSoup
import re
from pykrx import stock as pykrx_stock

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
    RUN_TIME = "16:00"                      # 오후 4시 (한국 기준)
    
    # 테스트 모드 (True 시 더미 데이터 사용)
    TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'


# ============================================================================
# 데이터 수집 (DATA FETCHER)
# ============================================================================

class StockDataFetcher:
    """주식 데이터 수집 클래스"""
    
    def __init__(self):
        self.session = requests.Session()
        self.timeout = Config.REQUEST_TIMEOUT
        self.retry_count = Config.REQUEST_RETRY
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    
    def get_kospi_kosdaq_list(self) -> List[Dict]:
        """
        KOSPI, KOSDAQ 종목 리스트 수집 (pykrx 사용)
        """
        try:
            logger.info("종목 리스트 수집 시작...")
            
            stocks = []
            
            # pykrx를 사용해 상장 종목 조회
            try:
                # 현재 상장된 모든 종목 조회
                ticker_list = pykrx_stock.get_market_ticker_list()
                
                for ticker in ticker_list:
                    # 종목명 조회
                    name = pykrx_stock.get_market_ticker_name(ticker)
                    
                    if name:
                        stocks.append({
                            'code': ticker,
                            'name': name
                        })
                
                if stocks:
                    logger.info(f"pykrx에서 {len(stocks)}개 종목 수집 완료")
                    return stocks
                    
            except Exception as e:
                logger.error(f"pykrx 종목 수집 실패: {e}")
            
            # 수집 실패 시 기본 리스트 사용
            logger.warning("종목 수집 실패, 기본 리스트로 대체")
            return self._get_default_stocks()
            
        except Exception as e:
            logger.error(f"종목 리스트 수집 실패: {e}")
            return []
    
    def get_stock_data(self, ticker: str) -> Optional[Dict]:
        """
        개별 종목 데이터 수집 (pykrx 사용)
        - 현재가, 전일 종가, 거래량
        """
        today_date = datetime.now().strftime('%Y%m%d')
        return self.get_stock_data_by_date(ticker, today_date)
    
    def get_stock_data_by_date(self, ticker: str, target_date: str) -> Optional[Dict]:
        """
        특정 날짜 종목 데이터 수집 (pykrx 사용)
        - 현재가, 전일 종가, 거래량
        """
        try:
            # 해당 날짜 데이터 조회
            try:
                ohlcv_data = pykrx_stock.get_market_ohlcv(target_date, target_date, ticker)
                
                if ohlcv_data is not None and not ohlcv_data.empty:
                    latest = ohlcv_data.iloc[-1]
                    
                    data = {
                        'ticker': ticker,
                        'current_price': int(latest['종가']),
                        'prev_close': int(latest['시가']),
                        'volume': int(latest['거래량']),
                        'prev_volume': int(latest['거래량']) // 2,
                        'foreign_cumulative': 0
                    }
                    
                    # 전일 종가 조회
                    try:
                        for prev_days in range(1, 20):
                            yesterday_date = (datetime.strptime(target_date, '%Y%m%d') - timedelta(days=prev_days)).strftime('%Y%m%d')
                            yesterday_data = pykrx_stock.get_market_ohlcv(
                                yesterday_date, yesterday_date, ticker
                            )
                            
                            if yesterday_data is not None and not yesterday_data.empty:
                                data['prev_close'] = int(yesterday_data.iloc[-1]['종가'])
                                data['prev_volume'] = int(yesterday_data.iloc[-1]['거래량'])
                                break
                    except:
                        pass
                    
                    logger.debug(f"수집 완료 {ticker}: 현가={data['current_price']}, 전일={data['prev_close']}, 거래량={data['volume']}")
                    return data
                    
            except:
                pass
            
            logger.debug(f"ticker {ticker}에 대한 데이터 없음")
            return None
            
        except Exception as e:
            logger.debug(f"종목 {ticker} 데이터 수집 실패: {e}")
            return None
    
    def _get_default_stocks(self) -> List[Dict]:
        """기본 종목 리스트 (테스트용)"""
        return [
            {'code': '005930', 'name': '삼성전자'},
            {'code': '000660', 'name': 'SK하이닉스'},
            {'code': '051910', 'name': 'LG화학'},
            {'code': '055550', 'name': '신한지주'},
            {'code': '096770', 'name': 'SK이노베이션'},
            {'code': '247540', 'name': '에코프로비엠'},
            {'code': '373220', 'name': 'LG에너지솔루션'},
            {'code': '099320', 'name': 'DB하이텍'},
            {'code': '068270', 'name': '셀트리온'},
            {'code': '207940', 'name': '삼성바이오로직스'},
        ]
    
    def _get_test_data(self, ticker: str) -> Optional[Dict]:
        """
        테스트용 더미 데이터
        """
        # 테스트 데이터셋
        test_stocks = {
            '005930': {'current': 161200, 'prev': 169000, 'volume': 12500000, 'prev_vol': 5000000, 'foreign': 50000},
            '000660': {'current': 880000, 'prev': 920000, 'volume': 10000000, 'prev_vol': 3500000, 'foreign': 30000},
            '051910': {'current': 314500, 'prev': 337000, 'volume': 8000000, 'prev_vol': 2800000, 'foreign': 20000},
            '055550': {'current': 98500, 'prev': 106000, 'volume': 12000000, 'prev_vol': 4200000, 'foreign': 45000},
            '096770': {'current': 109800, 'prev': 118000, 'volume': 6500000, 'prev_vol': 2100000, 'foreign': 15000},
        }
        
        if ticker in test_stocks:
            test = test_stocks[ticker]
            return {
                'ticker': ticker,
                'current_price': test['current'],
                'prev_close': test['prev'],
                'volume': test['volume'],
                'prev_volume': test['prev_vol'],
                'foreign_cumulative': test['foreign']
            }
        
        # 기타 종목은 더미 데이터
        import hashlib
        # Deterministic한 데이터 생성
        hash_val = int(hashlib.md5(ticker.encode()).hexdigest(), 16)
        
        base_price = 50000 + (hash_val % 500000)
        change_pct = ((hash_val % 10) - 3) / 100  # -3% ~ +7%
        
        return {
            'ticker': ticker,
            'current_price': int(base_price * (1 + change_pct)),
            'prev_close': base_price,
            'volume': 1000000 + (hash_val % 5000000),
            'prev_volume': int((1000000 + (hash_val % 5000000)) * 0.6),
            'foreign_cumulative': (hash_val % 100000) - 50000
        }
    
    def get_foreign_buy_data(self, ticker: str) -> Tuple[float, float]:
        """
        외국인 매매 데이터 수집 (pykrx 사용)
        (10일 누적 순매수)
        """
        today_date = datetime.now().strftime('%Y%m%d')
        return self.get_foreign_buy_data_by_date(ticker, today_date)
    
    def get_foreign_buy_data_by_date(self, ticker: str, target_date: str) -> Tuple[float, float]:
        """
        특정 날짜 기준 외국인 매매 데이터 수집 (pykrx 사용)
        (10일 누적 순매수)
        """
        try:
            # 10일 전부터 해당 날짜까지 누적 순매수 조회
            end_date = target_date
            start_date = (datetime.strptime(target_date, '%Y%m%d') - timedelta(days=15)).strftime('%Y%m%d')
            
            try:
                # pykrx: 투자자별 거래량 조회 (기간 합계)
                investor_data = pykrx_stock.get_market_trading_volume_by_investor(
                    start_date, end_date, ticker
                )
                
                if investor_data is None or investor_data.empty:
                    logger.debug(f"[{ticker}] 외국인 데이터 없음")
                    return 0, 0
                
                # 외국인 10일 누적 순매수 추출
                if '외국인' in investor_data.index:
                    foreign_net_buy = int(investor_data.loc['외국인', '순매수'])
                    
                    logger.debug(f"[{ticker}] 외국인 10일 누적 순매수: {foreign_net_buy}")
                    
                    return foreign_net_buy, foreign_net_buy
                    
            except Exception as e:
                logger.debug(f"외국인 데이터 추출 오류: {e}")
            
            return 0, 0
            
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
            if stock_data.get('foreign_cumulative', 0) > 0:
                conditions_met['foreign_buy'] = True
                scores['foreign_buy'] = min(100, int(stock_data['foreign_cumulative'] / 1000))
            
            # 3가지 모두 만족 여부
            all_conditions_met = all(conditions_met.values())
            
            return all_conditions_met, {
                'conditions': conditions_met,
                'scores': scores,
                'total_score': sum(scores.values()) // 3  # 평균 스코어
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
    def classify_signal_strength(filter_result: Dict) -> str:
        """신호 강도 분류"""
        try:
            score = filter_result.get('total_score', 0)
            
            if score >= 80:
                return "🔴 강한 매수 신호"
            elif score >= 60:
                return "📊 중간 매수 신호"
            else:
                return "📈 약한 신호"
                
        except Exception as e:
            logger.error(f"신호 분류 오류: {e}")
            return "❓ 불명"
    
    @staticmethod
    def format_stock_message(ticker: str, stock_data: Dict, name: str) -> str:
        """종목 메시지 포맷팅"""
        try:
            change_pct = ((stock_data.get('current_price', 0) - stock_data.get('prev_close', 0)) / stock_data.get('prev_close', 1) * 100)
            message = f"✅ {name} ({ticker})\n현재가: {stock_data.get('current_price', 'N/A')}원 | 변화율: {change_pct:.2f}%"
            
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
    
    def send_summary(self, results: List[Dict]) -> None:
        """요약 메시지 발송"""
        try:
            if not results:
                message = "📊 오늘 신호 있는 종목이 없습니다."
            else:
                message = f"🔍 스캔 결과: {len(results)}개 종목 발견\n\n"
                
                for result in results[:10]:  # 상위 10개만
                    message += result.get('message', '') + "\n\n"
                
                if len(results) > 10:
                    message += f"\n... 외 {len(results) - 10}개 종목"
            
            self.send_message(message)
            
        except Exception as e:
            logger.error(f"요약 메시지 발송 오류: {e}")


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
        self.results = []
    
    def scan(self, scan_date: str = None) -> List[Dict]:
        """전종목 스캔 실행"""
        try:
            logger.info("=" * 60)
            logger.info(f"스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("=" * 60)
            
            self.results = []
            
            # 스캔 날짜 설정 (기본값: 오늘)
            if scan_date is None:
                scan_date = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            logger.info(f"기준 날짜: {scan_date}")
            
            # 1. 종목 리스트 수집
            stocks = self.fetcher.get_kospi_kosdaq_list()
            if not stocks:
                logger.warning("종목 리스트를 수집할 수 없습니다.")
                stocks = self.fetcher._get_default_stocks()  # Fallback
            
            logger.info(f"검사할 종목 수: {len(stocks)}")
            
            # 2. 종목별 검사
            for idx, stock_info in enumerate(stocks):
                ticker = stock_info.get('code')
                name = stock_info.get('name', ticker)
                
                # 데이터 수집 (테스트 모드 또는 실제)
                if Config.TEST_MODE:
                    stock_data = self.fetcher._get_test_data(ticker)
                else:
                    # 특정 날짜 데이터 조회
                    stock_data = self.fetcher.get_stock_data_by_date(ticker, scan_date)
                    if not stock_data:
                        logger.debug(f"[{idx+1}] {name} - 데이터 수집 실패")
                        continue
                    
                    # 외국인 데이터 수집 (스캔 날짜 기준)
                    today_buy, cumulative = self.fetcher.get_foreign_buy_data_by_date(ticker, scan_date)
                    stock_data['foreign_buy'] = today_buy
                    stock_data['foreign_cumulative'] = cumulative
                
                logger.debug(f"[{idx+1}] {name}: 현가={stock_data.get('current_price')}, 전일={stock_data.get('prev_close')}, 거래량={stock_data.get('volume')}")
                
                # 3가지 조건 검사
                conditions_met, filter_result = self.filter.check_conditions(stock_data)
                
                if conditions_met:
                    # 메시지 생성
                    message = self.analyzer.format_stock_message(
                        ticker, stock_data, name
                    )
                    
                    self.results.append({
                        'ticker': ticker,
                        'name': name,
                        'message': message,
                        'score': filter_result.get('total_score', 0)
                    })
                    
                    logger.info(f"✓ [{idx+1}/{len(stocks)}] {name} ({ticker})")
                
                # 진행률 표시
                if (idx + 1) % 500 == 0:
                    logger.info(f"  >> 진행: {idx+1}/{len(stocks)} ({(idx+1)/len(stocks)*100:.1f}%)")
                
                # API 과부하 방지
                if (idx + 1) % 100 == 0:
                    time.sleep(1)
            
            # 결과 정렬 (스코어 기준)
            self.results.sort(key=lambda x: x['score'], reverse=True)
            
            logger.info(f"스캔 완료: {len(self.results)}개 종목 발견")
            
            return self.results
            
        except Exception as e:
            logger.error(f"스캔 오류: {e}", exc_info=True)
            return []
    
    def execute(self) -> None:
        """전체 실행"""
        try:
            # 스캔 실행
            results = self.scan()
            
            # 결과 텔레그램 발송
            if results:
                logger.info(f"결과 발송: {len(results)}개 종목")
                self.telegram.send_summary(results)
            else:
                self.telegram.send_message("📊 오늘은 신호가 없습니다.")
            
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
        # 매일 오후 4시 (16:00) 실행
        self.scheduler.add_job(
            self.run_scan,
            CronTrigger(hour=16, minute=0),
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
        
        scanner = StockScanner()
        results = scanner.scan(scan_date=scan_date)
        
        if results:
            logger.info(f"결과 발송: {len(results)}개 종목")
            scanner.telegram.send_summary(results)
        else:
            scanner.telegram.send_message(f"📊 {scan_date} 기준으로 신호가 없습니다.")
        
        logger.info("=" * 60)
        logger.info("실행 완료")
        logger.info("=" * 60)
    else:
        # 스케줄 모드: 매일 오후 4시 자동 실행
        scheduler.start()


if __name__ == "__main__":
    main()
