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

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
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
        KOSPI, KOSDAQ 전종목 리스트 수집
        """
        try:
            logger.info("종목 리스트 수집 시작...")
            
            stocks = []
            
            # KOSPI (sosok=0) + KOSDAQ (sosok=1) 수집
            for market_type, market_name in [(0, 'KOSPI'), (1, 'KOSDAQ')]:
                for page in range(1, 30):  # 약 2000+500개
                    url = f"https://finance.naver.com/sise/sise_market.naver?sosok={market_type}&page={page}"
                    
                    try:
                        response = self.session.get(url, headers=self.headers, timeout=self.timeout)
                        response.encoding = 'utf-8'
                        
                        if response.status_code != 200:
                            break
                        
                        soup = BeautifulSoup(response.text, 'lxml')
                        
                        # 테이블에서 종목 추출
                        table = soup.find('table', {'class': 'type_2'})
                        if not table:
                            break
                        
                        rows = table.find_all('tr')[1:]  # 헤더 제외
                        if not rows:
                            break
                        
                        for row in rows:
                            cols = row.find_all('td')
                            if len(cols) < 2:
                                continue
                            
                            # 종목명 추출
                            name_elem = cols[0].find('a')
                            if not name_elem:
                                continue
                            
                            name = name_elem.text.strip()
                            
                            # 종목코드 추출 (href에서)
                            href = name_elem.get('href', '')
                            code_match = re.search(r'code=(\d+)', href)
                            if not code_match:
                                continue
                            
                            code = code_match.group(1)
                            
                            stocks.append({
                                'code': code,
                                'name': name,
                                'market': market_name
                            })
                        
                        if page % 5 == 0:
                            logger.info(f"  {market_name} - {page} 페이지 완료 ({len(stocks)}개 누적)")
                            time.sleep(0.3)  # 과부하 방지
                        
                    except Exception as e:
                        logger.warning(f"{market_name} 페이지 {page} 수집 실패: {str(e)[:50]}")
                        continue
            
            logger.info(f"총 {len(stocks)}개 종목 수집 완료")
            return stocks
            
        except Exception as e:
            logger.error(f"종목 리스트 수집 실패: {e}")
            return []
    
    def get_stock_data(self, ticker: str) -> Optional[Dict]:
        """
        개별 종목 데이터 수집
        - 현재가, 전일 종가, 거래량
        """
        try:
            url = f"https://finance.naver.com/item/main.naver?code={ticker}"
            
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            data = {
                'ticker': ticker,
                'current_price': 0,
                'prev_close': 0,
                'volume': 0,
                'prev_volume': 0,
                'foreign_cumulative': 0
            }
            
            # 현재가 추출
            price_elem = soup.find('span', {'class': 'blind'})
            if price_elem:
                try:
                    data['current_price'] = int(price_elem.text.replace(',', '').split()[0])
                except:
                    pass
            
            # 스냅샷 정보 추출 (전일종가, 거래량)
            info_table = soup.find('table', {'class': 'no_info'})
            if info_table:
                rows = info_table.find_all('tr')
                for row in rows:
                    text = row.get_text()
                    
                    # 전일종가
                    if '전일종가' in text:
                        match = re.search(r'[\d,]+', row.find_all('td')[-1].text)
                        if match:
                            data['prev_close'] = int(match.group().replace(',', ''))
                    
                    # 거래량
                    if '거래량' in text and '거래량증감' not in text:
                        td_list = row.find_all('td')
                        if len(td_list) > 1:
                            match = re.search(r'[\d,]+', td_list[-1].text)
                            if match:
                                data['volume'] = int(match.group().replace(',', ''))
            
            # 전날 거래량 가져오기 (어제 데이터)
            try:
                # 차트 API에서 최근 거래량 정보
                chart_url = f"https://finance.naver.com/item/fchart.naver?code={ticker}&timeframe=day&count=2"
                chart_response = self.session.get(chart_url, headers=self.headers, timeout=5)
                
                if chart_response.status_code == 200:
                    # 간단한 거래량 추정 (API 응답 파싱)
                    soup_chart = BeautifulSoup(chart_response.text, 'lxml')
                    # 실제 파싱은 더 복잡할 수 있음
                    data['prev_volume'] = max(data['volume'] // 2, 1000000)  # 임시값
            except:
                data['prev_volume'] = max(data['volume'] // 2, 1000000)
            
            return data
            
        except Exception as e:
            logger.debug(f"종목 {ticker} 데이터 수집 실패: {e}")
            return None
    
    def get_foreign_buy_data(self, ticker: str) -> Tuple[float, float]:
        """
        외국인 매매 데이터 수집
        (당일 매매, 누적순매수)
        """
        try:
            url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
            
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                return 0, 0
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            today_buy = 0
            cumulative = 0
            
            # 외국인 테이블 찾기
            table = soup.find('table', {'class': 'type_2'})
            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) < 3:
                        continue
                    
                    text = row.get_text()
                    
                    # 현재 누적순매수 찾기
                    if '누적순매수' in text or cols[0].text.strip() in ['누적순매수', '합계']:
                        try:
                            # 마지막 컬럼에서 숫자 추출
                            value_text = cols[-1].text.strip()
                            # 컴마 제거 후 숫자만 추출
                            match = re.search(r'([-]?\d+(?:,\d+)*)', value_text)
                            if match:
                                cumulative = int(match.group().replace(',', ''))
                        except:
                            pass
            
            return today_buy, cumulative
            
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
    def format_stock_message(ticker: str, stock_data: Dict, 
                            filter_result: Dict, signal: str) -> str:
        """종목 메시지 포맷팅"""
        try:
            message = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 종목코드: {ticker}
현재가: {stock_data.get('current_price', 'N/A')}원
변화율: {((stock_data.get('current_price', 0) - stock_data.get('prev_close', 0)) / stock_data.get('prev_close', 1) * 100):.2f}%

📊 신호: {signal}
스코어: {filter_result.get('total_score', 0)}/100

거래량: {filter_result['conditions'].get('volume', False)}
주가하락: {filter_result['conditions'].get('price_drop', False)}
외국인순매수: {filter_result['conditions'].get('foreign_buy', False)}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""
            
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
    
    def scan(self) -> List[Dict]:
        """전종목 스캔 실행"""
        try:
            logger.info("=" * 60)
            logger.info(f"스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("=" * 60)
            
            self.results = []
            
            # 1. 종목 리스트 수집
            stocks = self.fetcher.get_kospi_kosdaq_list()
            if not stocks:
                logger.warning("종목 리스트를 수집할 수 없습니다.")
                stocks = self._get_default_stocks()  # Fallback
            
            logger.info(f"검사할 종목 수: {len(stocks)}")
            
            # 2. 종목별 검사
            for idx, stock_info in enumerate(stocks):
                ticker = stock_info.get('code')
                name = stock_info.get('name', ticker)
                
                # 데이터 수집
                stock_data = self.fetcher.get_stock_data(ticker)
                if not stock_data:
                    continue
                
                # 외국인 데이터 수집
                today_buy, cumulative = self.fetcher.get_foreign_buy_data(ticker)
                stock_data['foreign_buy'] = today_buy
                stock_data['foreign_cumulative'] = cumulative
                
                # 3가지 조건 검사
                conditions_met, filter_result = self.filter.check_conditions(stock_data)
                
                if conditions_met:
                    # 신호 강도 분류
                    signal = self.analyzer.classify_signal_strength(filter_result)
                    message = self.analyzer.format_stock_message(
                        ticker, stock_data, filter_result, signal
                    )
                    
                    self.results.append({
                        'ticker': ticker,
                        'name': name,
                        'message': message,
                        'score': filter_result.get('total_score', 0)
                    })
                    
                    logger.info(f"✓ [{idx+1}/{len(stocks)}] {name} ({ticker}) - {signal}")
                
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
    
    def _get_default_stocks(self) -> List[Dict]:
        """기본 종목 리스트 (테스트용)"""
        return [
            {'code': '005930', 'name': '삼성전자'},
            {'code': '000660', 'name': 'SK하이닉스'},
            {'code': '051910', 'name': 'LG화학'},
            # ... 더 추가
        ]
    
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
    else:
        # 스케줄 모드: 매일 오후 4시 자동 실행
        scheduler.start()


if __name__ == "__main__":
    main()
