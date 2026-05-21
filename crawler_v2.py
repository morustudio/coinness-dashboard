# crawler_v2.py — 코인니스 선물 지표 수집기
# ── 버전 히스토리 ──────────────────────────────────────────
# v2.0   : 최초 작성 (시간대 4행 → 1행, 21회/일)
# v2.1   : 정산 기준 시각 4개 시간대 수집 추가, 항목별 재시도
# v2.002 : 김치프리미엄 수집 추가 (업비트 KRW + 환율 API)
# v2.003 : 테이커 매수비율 + 청산 밀집 구간 수집 추가 (바이낸스 공개 API)
# ──────────────────────────────────────────────────────────
VERSION = "v2.003"

import os
import time
import json
import logging
import schedule
import subprocess
import urllib.request
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ── 로깅 ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crawler_v2.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── 설정 ───────────────────────────────────────────────────
OUTPUT_FILE  = "funding_data_v2.csv"
MAX_RETRIES  = 3
ITEM_RETRIES = 2
RETRY_DELAY  = 15
ITEM_DELAY   = 8

FUNDING_HOURS = [1, 9, 17]

SCHEDULE_TIMES = [
    "00:00", "00:30",
    "01:00",
    "01:30", "02:00",
    "04:00", "06:00",
    "08:00", "08:30",
    "09:00",
    "09:30", "10:00",
    "12:00", "14:00",
    "16:00", "16:30",
    "17:00",
    "17:30", "18:00",
    "20:00", "22:00",
]

ALL_TIME_FRAMES  = ["1시간", "4시간", "12시간", "24시간"]
BASE_TIME_FRAMES = ["1시간"]

FUNDING_EXCHANGES = ["바이낸스", "OKX", "바이비트"]

REQUIRED_FIELDS = [
    "롱숏_롱", "롱숏_숏",
    "청산금액", "청산비율",
    "펀딩_바이낸스", "펀딩_OKX", "펀딩_바이비트",
    "미결제_BTC", "BTC_현재가",
]


# ── 타이밍 / 정산회차 자동 계산 ───────────────────────────
def get_timing_and_session(dt: datetime) -> tuple:
    h, m = dt.hour, dt.minute
    total_min = h * 60 + m
    names = ["1차", "2차", "3차"]
    for idx, fh in enumerate(FUNDING_HOURS):
        fm = fh * 60
        diff = total_min - fm
        session = f"{names[idx]}({fh:02d}:00)"
        if diff == -60: return "정산_1시간전", session
        if diff == -30: return "정산_30분전",  session
        if diff ==   0: return "정산_기준",    session
        if diff ==  30: return "정산_30분후",  session
        if diff ==  60: return "정산_1시간후", session
    return "일반", "-"


def is_funding_time(dt: datetime) -> bool:
    return dt.hour in FUNDING_HOURS and dt.minute == 0


# ── 드라이버 ───────────────────────────────────────────────
def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver


def wait_page(driver, css: str, timeout: int = 30) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, css)) > 0
        )
        time.sleep(2)
        return True
    except TimeoutException:
        return False


def click_tf_btn(driver, tf: str) -> bool:
    for btn in driver.find_elements(By.TAG_NAME, "button"):
        if btn.text.strip() == tf:
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)
            return True
    return False


# ── 파서 1: 롱숏비율 ─────────────────────────────────────
def parse_long_short(driver, time_frames: list) -> list:
    results = []
    try:
        driver.get("https://coinness.com/market/future/long-short")
        if not wait_page(driver, "h1"):
            log.warning("  [롱숏] 페이지 로드 실패")
            return results
        for tf in time_frames:
            if not click_tf_btn(driver, tf):
                log.warning(f"  [롱숏][{tf}] 버튼 없음")
                continue
            h1s = [el.text.strip() for el in driver.find_elements(By.TAG_NAME, "h1") if "%" in el.text]
            if len(h1s) >= 2:
                results.append({"시간대": tf, "롱숏_롱": h1s[0], "롱숏_숏": h1s[1]})
                log.info(f"  [롱숏][{tf}] 롱={h1s[0]} 숏={h1s[1]}")
            else:
                log.warning(f"  [롱숏][{tf}] 값 부족")
    except Exception as e:
        log.error(f"  [롱숏] 오류: {e}")
    return results


# ── 파서 2: 강제청산 ──────────────────────────────────────
def parse_liquidations(driver, time_frames: list) -> list:
    results = []
    try:
        driver.get("https://coinness.com/market/future/liquidations")
        if not wait_page(driver, "div[class*='ShortCutBoxContainer']"):
            log.warning("  [청산] 페이지 로드 실패")
            return results
        for tf in time_frames:
            if not click_tf_btn(driver, tf):
                log.warning(f"  [청산][{tf}] 버튼 없음")
                continue
            row = {"시간대": tf}
            for box in driver.find_elements(By.CSS_SELECTOR, "div[class*='ShortCutBoxContainer']"):
                try:
                    label = box.find_element(By.TAG_NAME, "h1").text.strip()
                    value = box.find_element(By.TAG_NAME, "p").text.strip()
                    if "청산금액" in label:                     row["청산금액"] = value
                    elif "롱포지션" in label:                   row["청산_롱"]  = value
                    elif "숏포지션" in label:                   row["청산_숏"]  = value
                    elif "청산" in label and "비율" in label:   row["청산비율"] = value
                except Exception:
                    continue
            results.append(row)
            log.info(f"  [청산][{tf}] {row}")
    except Exception as e:
        log.error(f"  [청산] 오류: {e}")
    return results


# ── 파서 3: 펀딩비율 ──────────────────────────────────────
def parse_funding(driver) -> dict:
    result = {}
    try:
        driver.get("https://coinness.com/market/future/funding")
        if not wait_page(driver, "div[class*='TokenListWrapper']"):
            log.warning("  [펀딩] 페이지 로드 실패")
            return result
        target = None
        for wrapper in driver.find_elements(By.CSS_SELECTOR, "div[class*='TokenListWrapper']"):
            try:
                h3 = wrapper.find_element(By.TAG_NAME, "h3")
                if any(k in h3.text for k in ["테더", "USDT", "USD"]):
                    target = wrapper
                    break
            except Exception:
                continue
        if not target:
            log.error("  [펀딩] USDT 섹션 없음")
            return result
        for card in target.find_elements(By.CSS_SELECTOR, "div.w-\\[185px\\]"):
            try:
                exchange = card.find_element(By.TAG_NAME, "img").get_attribute("alt").strip()
                if exchange not in FUNDING_EXCHANGES:
                    continue
                fill_val = card.find_element(By.TAG_NAME, "svg").get_attribute("fill") or ""
                raw = card.find_element(By.TAG_NAME, "p").text.strip()
                num_str = raw.replace("%", "").strip()
                if "system-red" in fill_val:    val = f"+{num_str}%"
                elif "system-blue" in fill_val: val = f"-{num_str}%"
                else:                           val = "0.000%"
                key = {"바이낸스": "펀딩_바이낸스", "OKX": "펀딩_OKX", "바이비트": "펀딩_바이비트"}[exchange]
                result[key] = val
                log.info(f"  [펀딩] {exchange}: {val}")
            except Exception as e:
                log.warning(f"  [펀딩] 카드 오류: {e}")
    except Exception as e:
        log.error(f"  [펀딩] 오류: {e}")
    return result


# ── 파서 4: 미결제약정 ────────────────────────────────────
def parse_open_interest(driver) -> dict:
    result = {}
    try:
        driver.get("https://coinness.com/market/future/open-interest")
        if not wait_page(driver, "div[class*='TableBodyRow']"):
            log.warning("  [미결제] 페이지 로드 실패")
            return result
        for row in driver.find_elements(By.CSS_SELECTOR, "div[class*='TableBodyRow']"):
            try:
                if "바이낸스" not in row.text:
                    continue
                pct = row.find_element(By.CSS_SELECTOR, "span[class*='BarProgressText']").text.strip()
                cells = row.find_elements(By.XPATH, ".//div[@style and contains(@style,'width: 160px')]")
                btc = cells[0].text.strip() if len(cells) > 0 else None
                usd = cells[1].text.strip() if len(cells) > 1 else None
                result = {"미결제_비중": pct, "미결제_BTC": btc, "미결제_USD": usd}
                log.info(f"  [미결제] {result}")
                break
            except Exception:
                continue
    except Exception as e:
        log.error(f"  [미결제] 오류: {e}")
    return result


# ── 파서 5: BTC 현재가 ────────────────────────────────────
def parse_btc_price(driver) -> str:
    try:
        driver.get("https://coinness.com/market/coin")
        if not wait_page(driver, "div[class*='TableBodyRow']"):
            return None
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if "해외시세" in btn.text:
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                break
        for row in driver.find_elements(By.CSS_SELECTOR, "div[class*='TableBodyRow']"):
            try:
                links = row.find_elements(By.TAG_NAME, "a")
                if any("/market/coin/BTC" in (l.get_attribute("href") or "") for l in links):
                    price = row.find_element(By.CSS_SELECTOR, "span[class*='PriceText']").text.strip()
                    log.info(f"  [BTC] {price}")
                    return price
            except Exception:
                continue
    except Exception as e:
        log.error(f"  [BTC] 오류: {e}")
    return None


# ── 파서 6: 김치프리미엄 ──────────────────────────────────
def parse_kimchi_premium() -> dict:
    result = {}
    try:
        # 1. 업비트 BTC/KRW
        req = urllib.request.Request(
            "https://api.upbit.com/v1/ticker?markets=KRW-BTC",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            krw_price = float(json.loads(res.read())[0]["trade_price"])

        # 2. 환율 (USD/KRW)
        with urllib.request.urlopen(
            "https://api.exchangerate-api.com/v4/latest/USD", timeout=10
        ) as res:
            usd_krw = float(json.loads(res.read())["rates"]["KRW"])

        # 3. 바이낸스 선물 BTC/USDT (fallback: 현물)
        try:
            with urllib.request.urlopen(
                "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=10
            ) as res:
                usd_price = float(json.loads(res.read())["price"])
        except Exception:
            with urllib.request.urlopen(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=10
            ) as res:
                usd_price = float(json.loads(res.read())["price"])

        # 4. 김프 계산
        kimchi = round((krw_price / (usd_price * usd_krw) - 1) * 100, 3)
        result = {
            "업비트_KRW":  f"{int(krw_price):,}",
            "달러환율":    f"{usd_krw:.2f}",
            "김치프리미엄": f"{kimchi:+.3f}%",
        }
        log.info(
            f"  [김프] 업비트={result['업비트_KRW']}원 | "
            f"환율={result['달러환율']} | 김프={result['김치프리미엄']}"
        )
    except Exception as e:
        log.error(f"  [김프] 오류: {e}")
    return result


# ── 파서 7: 테이커 매수/매도 비율 ────────────────────────────
def parse_taker_ratio() -> dict:
    try:
        with urllib.request.urlopen(
            "https://fapi.binance.com/futures/data/takerlongshortRatio"
            "?symbol=BTCUSDT&period=5m&limit=1", timeout=10
        ) as res:
            data = json.loads(res.read())
            if data:
                buy_vol  = float(data[0]["buyVol"])
                sell_vol = float(data[0]["sellVol"])
                total    = buy_vol + sell_vol
                if total > 0:
                    buy_pct = round(buy_vol / total * 100, 1)
                    log.info(f"  [테이커] 매수={buy_pct}%")
                    return {"테이커_매수비율": f"{buy_pct}%"}
    except Exception as e:
        log.error(f"  [테이커] 오류: {e}")
    return {}


# ── 파서 8: 오더북 매수/매도벽 ──────────────────────────────
def parse_orderbook_walls() -> dict:
    try:
        with urllib.request.urlopen(
            "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=500", timeout=10
        ) as res:
            data = json.loads(res.read())

        def find_walls(orders, bucket=500):
            buckets = {}
            for price_str, qty_str in orders:
                price = float(price_str)
                qty   = float(qty_str)
                key   = round(price / bucket) * bucket
                buckets[key] = buckets.get(key, 0) + qty
            top = sorted(buckets.items(), key=lambda x: -x[1])[:1]
            return [(f"${int(p):,}", f"{round(q, 1)}BTC") for p, q in top]

        bid_walls = find_walls(data.get("bids", []))
        ask_walls = find_walls(data.get("asks", []))

        result = {}
        if bid_walls:
            result["매수벽_1가격"] = bid_walls[0][0]
            result["매수벽_1BTC"]  = bid_walls[0][1]
        if ask_walls:
            result["매도벽_1가격"] = ask_walls[0][0]
            result["매도벽_1BTC"]  = ask_walls[0][1]
        log.info(f"  [오더북] 매수벽={bid_walls} 매도벽={ask_walls}")
        return result
    except Exception as e:
        log.error(f"  [오더북] 오류: {e}")
    return {}


# ── 검증 ───────────────────────────────────────────────────
def validate(row: dict) -> list:
    return [f for f in REQUIRED_FIELDS if not row.get(f)]


# ── 저장 ───────────────────────────────────────────────────
def save_rows(rows: list) -> None:
    df = pd.DataFrame(rows)
    file_exists = os.path.exists(OUTPUT_FILE)
    df.to_csv(OUTPUT_FILE, index=False, mode="a",
              header=not file_exists, encoding="utf-8-sig")
    log.info(f"  저장 완료: {len(rows)}행 → {OUTPUT_FILE}")


# ── GitHub Push ────────────────────────────────────────────
def git_push(now_str: str) -> None:
    try:
        subprocess.run(["git", "add", OUTPUT_FILE], check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True
        )
        if not result.stdout.strip():
            log.info("  GitHub: 변경사항 없음, push 생략")
            return
        subprocess.run(["git", "commit", "-m", f"data: {now_str}"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        log.info("  ✅ GitHub push 완료")
    except FileNotFoundError:
        log.warning("  GitHub push 실패: git 명령어를 찾을 수 없음")
    except subprocess.CalledProcessError as e:
        log.error(f"  GitHub push 실패: {e}")


# ── 메인 수집 ─────────────────────────────────────────────
def run_collection() -> None:
    now = datetime.now()
    timing, session = get_timing_and_session(now)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    is_settlement = is_funding_time(now)
    time_frames = ALL_TIME_FRAMES if is_settlement else BASE_TIME_FRAMES
    tf_label = "전체 시간대" if is_settlement else "1시간"
    log.info(f"===== 수집 시작: {now_str} | {session} | {timing} | {tf_label} =====")

    for attempt in range(1, MAX_RETRIES + 1):
        driver = None
        try:
            driver = make_driver()

            # ── 펀딩비율 ──────────────────────────────────
            log.info("[1] 펀딩비율 수집")
            funding_data = {}
            for i in range(ITEM_RETRIES + 1):
                funding_data = parse_funding(driver)
                if all(funding_data.get(k) for k in ["펀딩_바이낸스", "펀딩_OKX", "펀딩_바이비트"]):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [펀딩] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            # ── 미결제약정 ────────────────────────────────
            log.info("[2] 미결제약정 수집")
            oi_data = {}
            for i in range(ITEM_RETRIES + 1):
                oi_data = parse_open_interest(driver)
                if oi_data.get("미결제_BTC"):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [미결제] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            # ── BTC 현재가 ────────────────────────────────
            log.info("[3] BTC 현재가 수집")
            btc_price = None
            for i in range(ITEM_RETRIES + 1):
                btc_price = parse_btc_price(driver)
                if btc_price:
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [BTC] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            # ── 롱숏비율 + 강제청산 (시간대별) ───────────
            log.info("[4] 롱숏비율 수집")
            ls_list = parse_long_short(driver, time_frames)
            log.info("[5] 강제청산 수집")
            liq_list = parse_liquidations(driver, time_frames)

            if not ls_list:
                raise ValueError("롱숏 데이터 수집 실패")

            # ── 김치프리미엄 (API, 드라이버 불필요) ───────
            log.info("[6] 김치프리미엄 수집")
            kimchi_data = {}
            for i in range(ITEM_RETRIES + 1):
                kimchi_data = parse_kimchi_premium()
                if kimchi_data.get("김치프리미엄"):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [김프] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)
            if not kimchi_data.get("김치프리미엄"):
                log.error("  [김프] 최종 실패")
                kimchi_data = {"업비트_KRW": None, "달러환율": None, "김치프리미엄": None}

            # ── 테이커 비율 (API, 드라이버 불필요) ────────
            log.info("[7] 테이커 매수비율 수집")
            taker_data = {}
            for i in range(ITEM_RETRIES + 1):
                taker_data = parse_taker_ratio()
                if taker_data.get("테이커_매수비율"):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [테이커] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)
            if not taker_data.get("테이커_매수비율"):
                log.error("  [테이커] 최종 실패")
                taker_data = {"테이커_매수비율": None}

            # ── 오더북 매수/매도벽 (API, 드라이버 불필요) ──
            log.info("[8] 오더북 매수/매도벽 수집")
            ob_data = {}
            for i in range(ITEM_RETRIES + 1):
                ob_data = parse_orderbook_walls()
                if ob_data.get("매수벽_1가격"):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [오더북] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)
            if not ob_data.get("매수벽_1가격"):
                log.error("  [오더북] 최종 실패")
                ob_data = {
                    "매수벽_1가격": None, "매수벽_1BTC": None,
                    "매수벽_2가격": None, "매수벽_2BTC": None,
                    "매도벽_1가격": None, "매도벽_1BTC": None,
                    "매도벽_2가격": None, "매도벽_2BTC": None,
                }

            # ── 행 조합 ──────────────────────────────────
            rows = []
            for tf in time_frames:
                ls  = next((x for x in ls_list  if x.get("시간대") == tf), {})
                liq = next((x for x in liq_list if x.get("시간대") == tf), {})
                row = {
                    "수집시간":      now_str,
                    "정산회차":      session,
                    "타이밍":       timing,
                    "시간대":       tf,
                    "롱숏_롱":      ls.get("롱숏_롱"),
                    "롱숏_숏":      ls.get("롱숏_숏"),
                    "청산금액":     liq.get("청산금액"),
                    "청산_롱":      liq.get("청산_롱"),
                    "청산_숏":      liq.get("청산_숏"),
                    "청산비율":     liq.get("청산비율"),
                    "펀딩_바이낸스": funding_data.get("펀딩_바이낸스"),
                    "펀딩_OKX":     funding_data.get("펀딩_OKX"),
                    "펀딩_바이비트": funding_data.get("펀딩_바이비트"),
                    "미결제_비중":   oi_data.get("미결제_비중"),
                    "미결제_BTC":   oi_data.get("미결제_BTC"),
                    "미결제_USD":   oi_data.get("미결제_USD"),
                    "BTC_현재가":   btc_price,
                    "업비트_KRW":   kimchi_data.get("업비트_KRW"),
                    "달러환율":     kimchi_data.get("달러환율"),
                    "김치프리미엄":  kimchi_data.get("김치프리미엄"),
                    "테이커_매수비율": taker_data.get("테이커_매수비율"),
                    "매수벽_1가격":  ob_data.get("매수벽_1가격"),
                    "매수벽_1BTC":   ob_data.get("매수벽_1BTC"),
                    "매수벽_2가격":  ob_data.get("매수벽_2가격"),
                    "매수벽_2BTC":   ob_data.get("매수벽_2BTC"),
                    "매도벽_1가격":  ob_data.get("매도벽_1가격"),
                    "매도벽_1BTC":   ob_data.get("매도벽_1BTC"),
                    "매도벽_2가격":  ob_data.get("매도벽_2가격"),
                    "매도벽_2BTC":   ob_data.get("매도벽_2BTC"),
                }
                rows.append(row)

            # ── 검증 ─────────────────────────────────────
            base_row = next((r for r in rows if r.get("시간대") == "1시간"), rows[0])
            missing = validate(base_row)
            if missing:
                log.warning(f"  누락 항목: {missing}")
                if attempt < MAX_RETRIES:
                    log.info(f"  전체 재시도 {attempt}/{MAX_RETRIES} ({RETRY_DELAY}초 후)")
                    driver.quit(); driver = None
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    log.error("  ❌ 최대 재시도 초과. 누락 포함 저장")

            save_rows(rows)
            git_push(now_str)
            log.info(f"===== 수집 완료: {len(rows)}행 =====\n")
            return

        except (TimeoutException, WebDriverException) as e:
            log.error(f"[시도 {attempt}] 브라우저 오류: {e}")
        except ValueError as e:
            log.error(f"[시도 {attempt}] {e}")
        except Exception as e:
            log.error(f"[시도 {attempt}] 예외: {e}", exc_info=True)
        finally:
            if driver:
                driver.quit()

        if attempt < MAX_RETRIES:
            log.info(f"  {RETRY_DELAY}초 후 재시도...")
            time.sleep(RETRY_DELAY)

    log.error(f"❌ 수집 최종 실패: {now_str}")


# ── 스케줄러 ──────────────────────────────────────────────
def setup_schedule() -> None:
    for t in SCHEDULE_TIMES:
        schedule.every().day.at(t).do(run_collection)
        log.info(f"  등록: {t}")
    log.info(f"  총 {len(SCHEDULE_TIMES)}개 스케줄 등록 완료")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        log.info(f"===== 크롤러 {VERSION} 테스트 모드: 즉시 1회 실행 =====")
        run_collection()
        sys.exit(0)

    log.info(f"===== 크롤러 {VERSION} 시작 =====")
    setup_schedule()
    log.info("대기 중... (Ctrl+C 로 종료)\n")
    while True:
        schedule.run_pending()
        time.sleep(30)
