# crawler_v2.py — 코인니스 선물 지표 수집기 v2.1
# 변경사항:
#   - 정산_기준 시각(01:00/09:00/17:00)에는 1h/4h/12h/24h 4개 시간대 수집 (4행)
#   - 나머지 시각은 1시간 기준 1행만 수집
#   - 하루 저장량: 21행 + 정산 3회×3행 = 30행/일
#   - 출력파일: funding_data_v2.csv

import os
import time
import logging
import schedule
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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

# 정산 기준 시각에만 4개 시간대 수집
ALL_TIME_FRAMES  = ["1시간", "4시간", "12시간", "24시간"]
BASE_TIME_FRAMES = ["1시간"]  # 일반 수집

FUNDING_EXCHANGES = ["바이낸스", "OKX", "바이비트"]

REQUIRED_FIELDS_SINGLE = [
    "롱숏_롱", "롱숏_숏",
    "청산금액", "청산비율",
    "펀딩_바이낸스", "펀딩_OKX", "펀딩_바이비트",
    "미결제_BTC", "BTC_현재가",
]


# ── 타이밍 / 정산회차 자동 계산 ───────────────────────────
def get_timing_and_session(dt: datetime) -> tuple[str, str]:
    h, m = dt.hour, dt.minute
    total_min = h * 60 + m
    names = ['1차', '2차', '3차']
    for idx, fh in enumerate(FUNDING_HOURS):
        fm = fh * 60
        diff = total_min - fm
        session = f"{names[idx]}({fh:02d}:00)"
        if diff == -60: return "정산_1시간전",  session
        if diff == -30: return "정산_30분전",   session
        if diff ==   0: return "정산_기준",     session
        if diff ==  30: return "정산_30분후",   session
        if diff ==  60: return "정산_1시간후",  session
    return "일반", "-"


def is_funding_time(dt: datetime) -> bool:
    """정산 기준 시각(01:00 / 09:00 / 17:00) 여부"""
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
def parse_long_short(driver, time_frames: list[str]) -> list[dict]:
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
def parse_liquidations(driver, time_frames: list[str]) -> list[dict]:
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
            boxes = driver.find_elements(By.CSS_SELECTOR, "div[class*='ShortCutBoxContainer']")
            for box in boxes:
                try:
                    label = box.find_element(By.TAG_NAME, "h1").text.strip()
                    value = box.find_element(By.TAG_NAME, "p").text.strip()
                    if "청산금액" in label:                        row["청산금액"] = value
                    elif "롱포지션" in label:                     row["청산_롱"]  = value
                    elif "숏포지션" in label:                     row["청산_숏"]  = value
                    elif "청산" in label and "비율" in label:     row["청산비율"] = value
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
                if "system-red" in fill_val:   val = f"+{num_str}%"
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
def parse_btc_price(driver) -> str | None:
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


# ── 검증 ───────────────────────────────────────────────────
def validate_row(row: dict) -> list[str]:
    return [f for f in REQUIRED_FIELDS_SINGLE if not row.get(f)]


# ── 저장 ───────────────────────────────────────────────────
def save_rows(rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    file_exists = os.path.exists(OUTPUT_FILE)
    df.to_csv(OUTPUT_FILE, index=False, mode="a",
              header=not file_exists, encoding="utf-8-sig")
    log.info(f"  저장 완료: {len(rows)}행 → {OUTPUT_FILE}")


# ── 메인 수집 ─────────────────────────────────────────────
def run_collection() -> None:
    now = datetime.now()
    timing, session = get_timing_and_session(now)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    is_settlement = is_funding_time(now)

    # 정산 기준 시각이면 4개 시간대, 그 외는 1시간만
    time_frames = ALL_TIME_FRAMES if is_settlement else BASE_TIME_FRAMES
    tf_label = "전체 시간대" if is_settlement else "1시간"
    log.info(f"===== 수집 시작: {now_str} | {session} | {timing} | {tf_label} =====")

    for attempt in range(1, MAX_RETRIES + 1):
        driver = None
        try:
            driver = make_driver()

            # ── 공통 데이터 수집 (시간대 무관) ──────────────
            funding_data = {}
            oi_data = {}
            btc_price = None

            for i in range(ITEM_RETRIES + 1):
                funding_data = parse_funding(driver)
                if all(funding_data.get(k) for k in ["펀딩_바이낸스","펀딩_OKX","펀딩_바이비트"]):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [펀딩] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            for i in range(ITEM_RETRIES + 1):
                oi_data = parse_open_interest(driver)
                if oi_data.get("미결제_BTC"):
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [미결제] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            for i in range(ITEM_RETRIES + 1):
                btc_price = parse_btc_price(driver)
                if btc_price:
                    break
                if i < ITEM_RETRIES:
                    log.warning(f"  [BTC] 재시도 {i+1}/{ITEM_RETRIES}")
                    time.sleep(ITEM_DELAY)

            # ── 시간대별 롱숏/청산 수집 ──────────────────────
            ls_list  = parse_long_short(driver, time_frames)
            liq_list = parse_liquidations(driver, time_frames)

            if not ls_list:
                raise ValueError("롱숏 데이터 수집 실패")

            # ── 행 조합 ──────────────────────────────────────
            rows = []
            for tf in time_frames:
                ls  = next((x for x in ls_list  if x.get("시간대") == tf), {})
                liq = next((x for x in liq_list if x.get("시간대") == tf), {})
                row = {
                    "수집시간":       now_str,
                    "정산회차":       session,
                    "타이밍":        timing,
                    "시간대":        tf,
                    "롱숏_롱":       ls.get("롱숏_롱"),
                    "롱숏_숏":       ls.get("롱숏_숏"),
                    "청산금액":      liq.get("청산금액"),
                    "청산_롱":       liq.get("청산_롱"),
                    "청산_숏":       liq.get("청산_숏"),
                    "청산비율":      liq.get("청산비율"),
                    "펀딩_바이낸스":  funding_data.get("펀딩_바이낸스"),
                    "펀딩_OKX":      funding_data.get("펀딩_OKX"),
                    "펀딩_바이비트":  funding_data.get("펀딩_바이비트"),
                    "미결제_비중":    oi_data.get("미결제_비중"),
                    "미결제_BTC":    oi_data.get("미결제_BTC"),
                    "미결제_USD":    oi_data.get("미결제_USD"),
                    "BTC_현재가":    btc_price,
                }
                rows.append(row)

            # ── 검증 (1시간 기준 행만 체크) ──────────────────
            base_row = next((r for r in rows if r.get("시간대") == "1시간"), rows[0])
            missing = validate_row(base_row)
            if missing:
                log.warning(f"  누락 항목: {missing}")
                if attempt < MAX_RETRIES:
                    log.info(f"  전체 재시도 {attempt}/{MAX_RETRIES} ({RETRY_DELAY}초 후)")
                    driver.quit(); driver = None
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    log.error(f"  ❌ 최대 재시도 초과. 누락 포함 저장")

            save_rows(rows)
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
        log.info("테스트 모드: 즉시 1회 실행")
        run_collection()
        sys.exit(0)

    log.info("===== 크롤러 v2.1 시작 =====")
    setup_schedule()
    log.info("대기 중... (Ctrl+C 로 종료)\n")
    while True:
        schedule.run_pending()
        time.sleep(30)

# 변경사항:
#   - 1행/수집 (시간대 4행 → 1행, 1시간 기준)
#   - 21회/일 (정산 전후 30분 추가, 일반 시간대 포함)
#   - 타이밍/정산회차 자동 계산
#   - 필수항목 검증 + 항목별 재시도
#   - 출력파일: funding_data_v2.csv

import os
import time
import logging
import schedule
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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
ITEM_RETRIES = 2       # 항목별 재시도 횟수
RETRY_DELAY  = 15      # 전체 재시도 대기(초)
ITEM_DELAY   = 8       # 항목별 재시도 대기(초)

# 바이낸스 펀딩피 정산 시각 (KST, 시 단위)
FUNDING_HOURS = [1, 9, 17]

# 수집 스케줄 (HH:MM)
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

FUNDING_EXCHANGES = ["바이낸스", "OKX", "바이비트"]

# 필수 항목 (빈값이면 재시도)
REQUIRED_FIELDS = [
    "롱숏_롱", "롱숏_숏",
    "청산금액", "청산비율",
    "펀딩_바이낸스", "펀딩_OKX", "펀딩_바이비트",
    "미결제_BTC", "BTC_현재가",
]


# ── 타이밍 / 정산회차 자동 계산 ───────────────────────────
def get_timing_and_session(dt: datetime) -> tuple[str, str]:
    h, m = dt.hour, dt.minute
    total_min = h * 60 + m

    for fh in FUNDING_HOURS:
        fm = fh * 60
        diff = total_min - fm
        if diff == -60: return "정산_1시간전",  f"{['1차','2차','3차'][FUNDING_HOURS.index(fh)]}({fh:02d}:00)"
        if diff == -30: return "정산_30분전",   f"{['1차','2차','3차'][FUNDING_HOURS.index(fh)]}({fh:02d}:00)"
        if diff ==   0: return "정산_기준",     f"{['1차','2차','3차'][FUNDING_HOURS.index(fh)]}({fh:02d}:00)"
        if diff ==  30: return "정산_30분후",   f"{['1차','2차','3차'][FUNDING_HOURS.index(fh)]}({fh:02d}:00)"
        if diff ==  60: return "정산_1시간후",  f"{['1차','2차','3차'][FUNDING_HOURS.index(fh)]}({fh:02d}:00)"

    return "일반", "-"


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


# ── 파서 1: 롱숏비율 (1시간 기준) ────────────────────────
def parse_long_short(driver) -> dict:
    result = {}
    try:
        driver.get("https://coinness.com/market/future/long-short")
        if not wait_page(driver, "h1"):
            log.warning("  [롱숏] 페이지 로드 실패")
            return result
        # 1시간 버튼 클릭
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if btn.text.strip() == "1시간":
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                break
        h1s = [el.text.strip() for el in driver.find_elements(By.TAG_NAME, "h1") if "%" in el.text]
        if len(h1s) >= 2:
            result["롱숏_롱"] = h1s[0]
            result["롱숏_숏"] = h1s[1]
            log.info(f"  [롱숏] 롱={h1s[0]} 숏={h1s[1]}")
        else:
            log.warning(f"  [롱숏] % 값 부족: {h1s}")
    except Exception as e:
        log.error(f"  [롱숏] 오류: {e}")
    return result


# ── 파서 2: 강제청산 (1시간 기준) ────────────────────────
def parse_liquidations(driver) -> dict:
    result = {}
    try:
        driver.get("https://coinness.com/market/future/liquidations")
        if not wait_page(driver, "div[class*='ShortCutBoxContainer']"):
            log.warning("  [청산] 페이지 로드 실패")
            return result
        # 1시간 버튼 클릭
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            if btn.text.strip() == "1시간":
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                break
        boxes = driver.find_elements(By.CSS_SELECTOR, "div[class*='ShortCutBoxContainer']")
        for box in boxes:
            try:
                label = box.find_element(By.TAG_NAME, "h1").text.strip()
                value = box.find_element(By.TAG_NAME, "p").text.strip()
                if "청산금액" in label:   result["청산금액"] = value
                elif "롱포지션" in label: result["청산_롱"]  = value
                elif "숏포지션" in label: result["청산_숏"]  = value
                elif "청산" in label and "비율" in label: result["청산비율"] = value
            except Exception:
                continue
        log.info(f"  [청산] {result}")
    except Exception as e:
        log.error(f"  [청산] 오류: {e}")
    return result


# ── 파서 3: 펀딩비율 ──────────────────────────────────────
def parse_funding(driver) -> dict:
    result = {}
    try:
        driver.get("https://coinness.com/market/future/funding")
        if not wait_page(driver, "div[class*='TokenListWrapper']"):
            log.warning("  [펀딩] 페이지 로드 실패")
            return result

        # USDT 섹션 찾기
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

                if "system-red" in fill_val:
                    val = f"+{num_str}%"
                elif "system-blue" in fill_val:
                    val = f"-{num_str}%"
                else:
                    val = "0.000%"

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
def parse_btc_price(driver) -> str | None:
    try:
        driver.get("https://coinness.com/market/coin")
        if not wait_page(driver, "div[class*='TableBodyRow']"):
            log.warning("  [BTC] 페이지 로드 실패")
            return None
        # 해외시세 버튼 클릭
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


# ── 검증 ───────────────────────────────────────────────────
def validate(row: dict) -> list[str]:
    """빈 필수 항목 목록 반환. 빈 리스트면 정상."""
    return [f for f in REQUIRED_FIELDS if not row.get(f)]


# ── 저장 ───────────────────────────────────────────────────
def save_data(row: dict) -> None:
    df = pd.DataFrame([row])
    file_exists = os.path.exists(OUTPUT_FILE)
    df.to_csv(OUTPUT_FILE, index=False, mode="a",
              header=not file_exists, encoding="utf-8-sig")
    log.info(f"  저장 완료 → {OUTPUT_FILE}")


# ── 메인 수집 ─────────────────────────────────────────────
def run_collection() -> None:
    now = datetime.now()
    timing, session = get_timing_and_session(now)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    log.info(f"===== 수집 시작: {now_str} | {session} | {timing} =====")

    for attempt in range(1, MAX_RETRIES + 1):
        driver = None
        try:
            driver = make_driver()
            row = {
                "수집시간": now_str,
                "정산회차": session,
                "타이밍":  timing,
            }

            # ── 항목별 수집 + 개별 재시도 ──────────────────
            collectors = [
                ("롱숏",   parse_long_short,     ["롱숏_롱", "롱숏_숏"]),
                ("청산",   parse_liquidations,   ["청산금액", "청산비율"]),
                ("펀딩",   parse_funding,        ["펀딩_바이낸스", "펀딩_OKX", "펀딩_바이비트"]),
                ("미결제", parse_open_interest,  ["미결제_BTC"]),
                ("BTC",    None,                 ["BTC_현재가"]),
            ]

            for name, func, keys in collectors:
                if name == "BTC":
                    # BTC는 별도 처리
                    for i in range(ITEM_RETRIES + 1):
                        price = parse_btc_price(driver)
                        if price:
                            row["BTC_현재가"] = price
                            break
                        if i < ITEM_RETRIES:
                            log.warning(f"  [BTC] 재시도 {i+1}/{ITEM_RETRIES}")
                            time.sleep(ITEM_DELAY)
                    if not row.get("BTC_현재가"):
                        log.error("  [BTC] 최종 실패 — None 저장")
                        row["BTC_현재가"] = None
                    continue

                for i in range(ITEM_RETRIES + 1):
                    data = func(driver)
                    row.update(data)
                    missing = [k for k in keys if not row.get(k)]
                    if not missing:
                        break
                    if i < ITEM_RETRIES:
                        log.warning(f"  [{name}] 누락={missing}, 재시도 {i+1}/{ITEM_RETRIES}")
                        time.sleep(ITEM_DELAY)
                    else:
                        log.error(f"  [{name}] 최종 실패 — 누락={missing}")

            # ── 검증 ────────────────────────────────────────
            missing_all = validate(row)
            if missing_all:
                log.warning(f"  누락 항목: {missing_all}")
                if attempt < MAX_RETRIES:
                    log.info(f"  전체 재시도 {attempt}/{MAX_RETRIES} ({RETRY_DELAY}초 후)")
                    driver.quit()
                    driver = None
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    log.error(f"  ❌ 최대 재시도 초과. 누락 포함 저장: {missing_all}")

            save_data(row)
            log.info(f"===== 수집 완료 =====\n")
            return

        except (TimeoutException, WebDriverException) as e:
            log.error(f"[시도 {attempt}] 브라우저 오류: {e}")
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
        log.info("테스트 모드: 즉시 1회 실행")
        run_collection()
        sys.exit(0)

    log.info("===== 크롤러 v2 시작 =====")
    setup_schedule()
    log.info("대기 중... (Ctrl+C 로 종료)\n")
    while True:
        schedule.run_pending()
        time.sleep(30)
