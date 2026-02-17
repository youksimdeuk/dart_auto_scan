# 📊 주식 자동화 스캐너

**매일 오후 4시 자동으로 전종목을 스캔하고, 조건에 맞는 종목을 텔레그램으로 보내줍니다.**

---

## 🎯 필터링 조건

3가지 조건을 모두 만족하는 종목을 발견하면 텔레그램으로 알림:

| 조건 | 값 |
|------|-----|
| ✅ **거래량** | 전날 대비 **2.5배 이상** |
| ✅ **주가** | **4% 이상 하락** |
| ✅ **외국인** | **순매수 누적 양수** |

---

## 🚀 설정 방법

### 1️⃣ 로컬 테스트 (선택사항)

```bash
# 저장소 클론
git clone https://github.com/YOUR_USERNAME/stock-scanner.git
cd stock-scanner

# 환경 설정
pip install -r requirements.txt

# 환경변수 설정 (.env 파일 또는 직접)
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="your_chat_id_here"

# 테스트 실행
python auto_stock.py --test
```

### 2️⃣ GitHub Actions 자동 실행 설정

#### Step 1: Secrets 설정

1. GitHub 저장소 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret** 클릭
3. 다음 2개 추가:

   ```
   TELEGRAM_BOT_TOKEN = 8553989410:AAEFswOVbdEBPdyHXR1LOkw4uOvJbRduROE
   TELEGRAM_CHAT_ID = 1249787976
   ```

#### Step 2: 워크플로우 자동 실행

- 워크플로우는 `평일 오후 4시`(한국 시간) 자동 실행
- 또는 **Actions** 탭에서 수동으로 "Run workflow" 클릭

---

## 📱 텔레그램 설정

### 텔레그램 봇 만들기

1. **@BotFather** 검색 → `/newbot`
2. 봇 이름 설정 (예: "stock-scanner-bot")
3. 받은 **토큰** 복사 → `TELEGRAM_BOT_TOKEN`

### 채팅 ID 확인

1. **@userinfobot** 검색 → `START` 클릭
2. 반환된 `Id:` 숫자 → `TELEGRAM_CHAT_ID`

---

## 📊 메시지 형식

스캔 결과 예시:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 종목코드: 005930
현재가: 70000원
변화율: -5.50%

📊 신호: 🔴 강한 매수 신호
스코어: 85/100

거래량: ✓ True
주가하락: ✓ True
외국인순매수: ✓ True
━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## ⚙️ 커스터마이징

### 필터 조건 수정

`auto_stock.py`의 `Config` 클래스에서:

```python
VOLUME_RATIO = 2.5          # 거래량 배수 조정
PRICE_DROP_PCT = -4.0       # 하락률 조정 (음수)
FOREIGN_BUY_SIGNAL = True   # 외국인 신호 켜기/끄기
```

### 실행 시간 수정

`.github/workflows/stock_scan.yml`:

```yaml
- cron: '0 7 * * MON-FRI'  # UTC 기준 (Korea = UTC+9)
# 오후 4시 = UTC 07:00 (한국 시간 기준)
```

---

## 🐛 문제 해결

### 텔레그램에 메시지가 안 옴

1. **Secrets 확인**: 토큰과 채팅 ID가 올바른지 확인
2. **봇 권한**: 봇이 채팅 권한이 있는지 확인
3. **GitHub Actions 로그**: Actions 탭에서 실행 로그 확인

### 스캔이 느림

- 전종목(~3000개) 스캔하므로 10-20분 소요
- 네이버 금융 서버 부하 시간대 피하기 권장

---

## 📈 성능

- **스캔 범위**: KOSPI + KOSDAQ (약 3,500개)
- **실행 시간**: 약 10-20분
- **API 호출**: 네이버 금융 (크롤링)

---

## ⚖️ 법적 고지

- 이 스크립트는 **데이터 수집 및 분석 목적**입니다
- 네이버 금융 이용약관 준수
- **투자 조언이 아닙니다** - 자신의 판단으로 투자하세요

---

## 📝 라이선스

MIT License

---

**질문이나 버그 제보**: Issues 탭에서 등록해주세요!
