from pykrx import stock as pykrx_stock
from datetime import datetime, timedelta

# 삼성전자 2월 13일 데이터
ticker = '005930'
target_date = '20260213'

print('=== 삼성전자(005930) 2월 13일 데이터 ===')
print()

# OHLCV 데이터
try:
    ohlcv = pykrx_stock.get_market_ohlcv(target_date, target_date, ticker)
    if ohlcv is not None and not ohlcv.empty:
        print('[OHLCV 데이터]')
        print(ohlcv)
        print()
        latest = ohlcv.iloc[-1]
        print(f'종가: {int(latest["종가"])}원')
        print(f'시가: {int(latest["시가"])}원')
        print(f'고가: {int(latest["고가"])}원')
        print(f'저가: {int(latest["저가"])}원')
        print(f'거래량: {int(latest["거래량"])}주')
        print()
    else:
        print('[OHLCV] 데이터 없음')
except Exception as e:
    print(f'OHLCV 오류: {e}')
    print()

# 외국인 데이터
try:
    start_date = (datetime.strptime(target_date, '%Y%m%d') - timedelta(days=15)).strftime('%Y%m%d')
    investor = pykrx_stock.get_market_trading_volume_by_investor(start_date, target_date, ticker)
    if investor is not None and not investor.empty:
        print('[투자자별 거래량]')
        print(investor)
        print()
        if '외국인' in investor.index:
            print(f'외국인 10일 누적 순매수: {int(investor.loc["외국인", "순매수"])}주')
    else:
        print('[투자자별] 데이터 없음')
except Exception as e:
    print(f'외국인 데이터 오류: {e}')
