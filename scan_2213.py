#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2월 13일 데이터 기준 스캔"""

import sys
from datetime import datetime, timedelta
from auto_stock import StockScanner

# 스캐너 초기화
scanner = StockScanner()

print('='*60)
print('2월 13일 기준 스캔 시작')
print('='*60 + '\n')

# 스캔 실행
results = scanner.scan()

print(f'\n발견된 종목: {len(results)}개\n')

if results:
    for r in results:
        print(f'✓ {r["name"]} ({r["ticker"]})')
        print(f'  └─ {r["message"]}\n')
else:
    print('발견된 종목이 없습니다.')

print('='*60)
