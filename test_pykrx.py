#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pykrx 테스트"""

from pykrx import stock
from datetime import datetime, timedelta

print("=== pykrx 테스트 ===\n")

# 테스트 1: 종목 리스트
print("1. 종목 리스트 조회")
try:
    tickers = stock.get_market_ticker_list()
    print(f"   ✓ {len(tickers)}개 종목")
except Exception as e:
    print(f"   ✗ 오류: {e}")

print()

# 테스트 2: 종목명
print("2. 종목명 조회 (삼성전자)")
try:
    name = stock.get_market_ticker_name('005930')
    print(f"   ✓ {name}")
except Exception as e:
    print(f"   ✗ 오류: {e}")

print()

# 테스트 3: OHLCV 데이터
print("3. OHLCV 데이터 (삼성전자, 최근 5일)")
try:
    target_date = (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')
    today_str = datetime.now().strftime('%Y%m%d')
    
    df = stock.get_market_ohlcv(target_date, today_str, '005930')
    if df is not None and not df.empty:
        print(f"   ✓ {len(df)}일 데이터 조회 성공")
        print(f"\n{df}\n")
    else:
        print(f"   ✗ 데이터 없음")
except Exception as e:
    print(f"   ✗ 오류: {e}")

print()

# 테스트 4: 외국인 거래량
print("4. 외국인 거래량 조회")
try:
    today_str = datetime.now().strftime('%Y%m%d')
    df = stock.get_market_net_purchases_of_equities_by_ticker(today_str, '005930')
    if df is not None and not df.empty:
        print(f"   ✓ 데이터 조회 성공")
        print(f"\n{df}\n")
    else:
        print(f"   ✗ 데이터 없음")
except Exception as e:
    print(f"   ✗ 오류: {e}")

print("=== 완료 ===")
