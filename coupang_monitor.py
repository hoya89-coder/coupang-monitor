"""
쿠팡 Wing API 모니터링 스크립트
- 일일 매출 집계 및 전일 대비 비교
- 판매중지 / 품질이슈 상품 감지
- Slack으로 리포트 + 긴급 알림 발송

환경 변수 (GitHub Secrets or .env):
  COUPANG_ACCESS_KEY     : Wing API Access Key
  COUPANG_SECRET_KEY     : Wing API Secret Key
  COUPANG_VENDOR_ID      : 판매자 ID (A숫자)
  SLACK_WEBHOOK_URL      : Slack Incoming Webhook URL
  SLACK_ALERT_CHANNEL    : 긴급 알림 채널 (예: #coupang-alerts)
"""

import os, hmac, hashlib, datetime, requests, json, time
from urllib.parse import urlencode

# ── 설정 ──────────────────────────────────────────────
ACCESS_KEY  = os.environ["COUPANG_ACCESS_KEY"]
SECRET_KEY  = os.environ["COUPANG_SECRET_KEY"]
VENDOR_ID   = os.environ["COUPANG_VENDOR_ID"]
WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
ALERT_CH    = os.environ.get("SLACK_ALERT_CHANNEL", "#coupang-alerts")

BASE_URL    = "https://api-gateway.coupang.com"

# 매출 급감 경고 임계값 (전일 대비 %)
SALES_DROP_THRESHOLD = -30


# ── HMAC 인증 헬퍼 ─────────────────────────────────────
def _utc_now():
    return datetime.datetime.utcnow().strftime("%y%m%dT%H%M%SZ")

def _sign(method, path, query, dt):
    msg = dt + method + path + query
    return hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()

def coupang_request(method, path, params=None):
    query = urlencode(params or {})
    dt    = _utc_now()
    sig   = _sign(method.upper(), path, query, dt)
    url   = f"{BASE_URL}{path}{'?' + query if query else ''}"
    headers = {
        "Authorization": (
            f"CEA algorithm=HmacSHA256, access-key={ACCESS_KEY}, "
            f"signed-date={dt}, signature={sig}"
        ),
        "Content-Type": "application/json;charset=UTF-8",
    }
    resp = requests.request(method, url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── API 호출 함수 ──────────────────────────────────────
def get_daily_sales(date_str: str) -> dict:
    """
    일별 매출 조회 (날짜: YYYY-MM-DD)
    """
    path = f"/v2/providers/seller_api/apis/api/v1/vendor/sales/revenue-history"
    data = coupang_request("GET", path, {
        "vendorId":  VENDOR_ID,
        "startDate": date_str,
        "endDate":   date_str,
    })
    items = data.get("data", {}).get("revenueHistoryList", [])
    total = sum(i.get("revenue", 0) for i in items)
    return {"total": total, "raw": items}


def get_product_list(status: str = "APPROVED", page_size: int = 100) -> list:
    """
    상품 목록 조회 — 판매 상태 필터링
    status: APPROVED(판매중) | PARTIAL(일부중지) | REJECTED(반려)
    """
    path  = f"/v2/providers/seller_api/apis/api/v1/vendor/products"
    items, next_token = [], None
    while True:
        params = {"vendorId": VENDOR_ID, "pageSize": page_size, "status": status}
        if next_token:
            params["nextToken"] = next_token
        data = coupang_request("GET", path, params)
        batch = data.get("data", {}).get("productList", [])
        items.extend(batch)
        next_token = data.get("data", {}).get("nextToken")
        if not next_token or not batch:
            break
        time.sleep(0.3)   # rate limit 방지
    return items


def get_stopped_products() -> list:
    """
    판매중지(STOP_SALE) 및 품질이슈 상품 감지
    Wing API의 판매중지 사유 코드를 함께 반환
    """
    path = f"/v2/providers/seller_api/apis/api/v1/vendor/products"
    data = coupang_request("GET", path, {
        "vendorId": VENDOR_ID,
        "pageSize": 100,
        "status":   "STOP_SALE",
    })
    return data.get("data", {}).get("productList", [])


# ── 분석 ──────────────────────────────────────────────
def analyze(today_sales, yesterday_sales, stopped, all_products) -> dict:
    today_rev     = today_sales["total"]
    yesterday_rev = yesterday_sales["total"]
    delta_pct = (
        ((today_rev - yesterday_rev) / yesterday_rev * 100) if yesterday_rev else 0
    )

    quality_issues = [
        p for p in stopped
        if "품질" in p.get("stopReason", "") or "QUALITY" in p.get("stopReasonCode", "")
    ]

    return {
        "today_rev":      today_rev,
        "yesterday_rev":  yesterday_rev,
        "delta_pct":      delta_pct,
        "total_products": len(all_products),
        "stopped_count":  len(stopped),
        "stopped":        stopped,
        "quality_issues": quality_issues,
        "is_critical":    len(stopped) > 0 or delta_pct <= SALES_DROP_THRESHOLD,
    }


# ── Slack 메시지 포맷 ──────────────────────────────────
def _fmt_krw(v: float) -> str:
    return f"{v:,.0f}원"

def _trend(pct: float) -> str:
    if pct >  5: return "📈"
    if pct < -5: return "📉"
    return "➡️"

def build_slack_payload(result: dict, report_date: str) -> dict:
    r = result
    sign = "+" if r["delta_pct"] >= 0 else ""
    color = "#2eb886" if not r["is_critical"] else "#e01e5a"

    # 헤더 블록
    blocks = [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"🛍️ 쿠팡 일일 판매 리포트 — {report_date}",
        }},
        {"type": "section", "fields": [
            {"type": "mrkdwn",
             "text": f"*오늘 매출*\n{_fmt_krw(r['today_rev'])}"},
            {"type": "mrkdwn",
             "text": f"*전일 대비*\n{_trend(r['delta_pct'])} {sign}{r['delta_pct']:.1f}%"},
            {"type": "mrkdwn",
             "text": f"*전체 상품 수*\n{r['total_products']:,}개"},
            {"type": "mrkdwn",
             "text": f"*판매중지 상품*\n{'⚠️ ' + str(r['stopped_count']) + '개' if r['stopped_count'] else '✅ 없음'}"},
        ]},
    ]

    # 판매중지 상품 상세
    if r["stopped"]:
        lines = []
        for p in r["stopped"][:10]:   # 최대 10개 표시
            name   = p.get("sellerProductName", "상품명 없음")[:30]
            reason = p.get("stopReason", p.get("stopReasonCode", "사유 미확인"))
            lines.append(f"• `{p.get('sellerProductId', '-')}` *{name}* — {reason}")
        if len(r["stopped"]) > 10:
            lines.append(f"_...외 {len(r['stopped']) - 10}개_")

        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": "*🚫 판매중지 상품 목록*\n" + "\n".join(lines),
        }})

    # 매출 급감 경고
    if r["delta_pct"] <= SALES_DROP_THRESHOLD:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": (
                f"*⚠️ 매출 급감 감지*\n"
                f"전일 대비 {r['delta_pct']:.1f}% 하락했습니다. "
                f"판매 채널 및 광고 상태를 확인해 주세요."
            ),
        }})

    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn",
         "text": f"_Wing 어드민 기준 | 기준일: {report_date} | 매출 급감 알림 임계값: {SALES_DROP_THRESHOLD}%_"}
    ]})

    payload = {
        "channel":     ALERT_CH,
        "attachments": [{"color": color, "blocks": blocks}],
    }

    # 긴급 상황 시 @channel 멘션 추가
    if r["is_critical"]:
        payload["text"] = "<!channel> 🚨 쿠팡 판매 이슈 발생 — 즉시 확인 필요"

    return payload


# ── 발송 ──────────────────────────────────────────────
def send_slack(payload: dict):
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Slack 발송 실패: {resp.status_code} {resp.text}")


# ── 메인 ──────────────────────────────────────────────
def main():
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    today_str     = today.strftime("%Y-%m-%d")
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    print(f"[{today_str}] 쿠팡 모니터링 시작")

    today_sales     = get_daily_sales(today_str)
    yesterday_sales = get_daily_sales(yesterday_str)
    stopped         = get_stopped_products()
    all_products    = get_product_list(status="APPROVED")

    result  = analyze(today_sales, yesterday_sales, stopped, all_products)
    payload = build_slack_payload(result, today_str)
    send_slack(payload)

    print(f"완료 — 매출: {_fmt_krw(result['today_rev'])}, "
          f"판매중지: {result['stopped_count']}개, "
          f"긴급: {result['is_critical']}")


if __name__ == "__main__":
    main()
