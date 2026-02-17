import requests
from bs4 import BeautifulSoup

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# KOSPI 시장 페이지
url = 'https://finance.naver.com/sise/'
response = requests.get(url, headers=headers, timeout=5)

soup = BeautifulSoup(response.text, 'html.parser')

# 링크 찾기
links = soup.find_all('a', href=True)
kospi_links = [l for l in links if '/item/' in l.get('href') and l.text.strip()]
print(f'종목 링크 개수: {len(kospi_links)}')

if kospi_links[:5]:
    for link in kospi_links[:5]:
        href = link.get('href')
        print(f'  {link.text}: {href}')
        
# 검색어로 종목 리스트 가져오기
print("\n\n=== 다른 접근 방식 ===")
import re

# 종목 코드 패턴 찾기
pattern = r'/item/main\.naver\?code=(\d+)'
matches = re.findall(pattern, response.text)
print(f'정규식으로 찾은 종목 코드: {len(matches)}개')
if matches[:5]:
    print(f'  샘플: {matches[:5]}')
