"""
每日同步（GitHub Action 运行），结果写入家庭持仓使用的同一个 Gist：
  1. bench_index_data.json   对比指数（中证官网）的日收盘点位
  2. fund_dividends.json     持仓里场内基金/ETF 的分红记录（东方财富·天天基金网“分红送配”页）
  3. 把已到除息日的基金分红，直接写进页面的云备份（family_stock_tracker_backup.json）的流水记录里，
     这样已经发出去的旧版 HTML 只要打开时从云端同步，就能看到这笔分红，不用换新文件

两部分互相独立：基金分红获取失败不会影响指数同步，只会在日志里给出警告。
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.request

# ---------------- 配置 ----------------
INDICES = {
    "930950": "中证偏股型基金指数",
    "000300": "沪深300",
}
START_DATE = os.environ.get("START_DATE", "20250101")
INDEX_FILE = "bench_index_data.json"
STATE_FILE = "family_stock_tracker_backup.json"   # 页面自己的云备份，用来读取持仓代码
DIV_FILE = "fund_dividends.json"
FALLBACK_FUND_CODES = [c.strip() for c in os.environ.get("FUND_CODES", "515180").split(",") if c.strip()]

GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_ID = os.environ.get("GIST_ID", "")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------- 通用请求 ----------------
def http_text(url, headers=None, data=None, method="GET", timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def http_json(url, headers=None, data=None, method="GET"):
    return json.loads(http_text(url, headers, data, method))


def gh_headers():
    return {
        "Authorization": "token " + GIST_TOKEN,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "family-stock-bench-sync",
    }


def get_gist():
    return http_json("https://api.github.com/gists/" + GIST_ID, gh_headers())


def gist_file_text(gist, name):
    f = (gist.get("files") or {}).get(name)
    if not f:
        return None
    if f.get("truncated") and f.get("raw_url"):
        return http_text(f["raw_url"], {"User-Agent": "family-stock-bench-sync"})
    return f.get("content")


def write_gist_file(name, payload, indent=None):
    content = (json.dumps(payload, ensure_ascii=False, indent=indent) if indent
               else json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    body = json.dumps({"files": {name: {"content": content}}}).encode("utf-8")
    http_json("https://api.github.com/gists/" + GIST_ID, gh_headers(), body, "PATCH")


# ---------------- 1. 对比指数 ----------------
def fetch_index(code):
    end = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y%m%d")
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           f"?indexCode={code}&startDate={START_DATE}&endDate={end}")
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.csindex.com.cn/",
        "Accept": "application/json, text/plain, */*",
    }
    for attempt in range(1, 4):
        try:
            j = http_json(url, headers)
            out = []
            for r in (j.get("data") or []):
                d, c = r.get("tradeDate"), r.get("close")
                if d and c not in (None, ""):
                    out.append([str(d).replace("-", ""), round(float(c), 4)])
            out.sort()
            if out:
                print(f"{code}: {len(out)} 条，最新 {out[-1][0]} = {out[-1][1]}")
                return out
            print(f"{code}: 第 {attempt} 次返回空数据")
        except Exception as e:
            print(f"{code}: 第 {attempt} 次失败：{e}")
        time.sleep(5)
    return []


def sync_indices(gist):
    old = {}
    try:
        txt = gist_file_text(gist, INDEX_FILE)
        old = (json.loads(txt) if txt else {}).get("indices", {})
    except Exception as e:
        print("读取旧指数数据失败（首次运行可忽略）：", e)

    indices, any_new = {}, False
    for code in INDICES:
        rows = fetch_index(code)
        if rows:
            indices[code] = rows
            any_new = True
        elif code in old:
            print(f"{code}: 本次失败，沿用旧数据")
            indices[code] = old[code]
    if not any_new:
        print("所有指数都没有拉到新数据，不写入 Gist")
        return False

    write_gist_file(INDEX_FILE, {
        "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "source": "csindex.com.cn",
        "names": INDICES,
        "indices": indices,   # { code: [[YYYYMMDD, close], ...] }
    })
    print("已写入 Gist：", INDEX_FILE)
    return True


# ---------------- 2. 场内基金分红 ----------------
def fund_codes_from_state(gist):
    """从页面的云备份里读出持仓中的场内基金代码；读不到就用 FUND_CODES 兜底。"""
    codes = []
    try:
        txt = gist_file_text(gist, STATE_FILE)
        state = json.loads(txt)["state"]
        for h in state.get("holdings", []):
            code = str(h.get("code", ""))
            is_fund = h.get("assetType") == "fund" or (not h.get("assetType") and code[:1] in ("1", "5"))
            if is_fund and len(code) == 6 and code.isdigit() and code not in codes:
                codes.append(code)
    except Exception as e:
        print("读取持仓代码失败，改用默认代码：", e)
    return codes or FALLBACK_FUND_CODES


# 表格列：年份 | 权益登记日 | 除息日 | 每份(或每10份)分红 | 分红发放日
ROW_RE = re.compile(
    r"(\d{4})年\s*(\d{4}-\d{2}-\d{2})\s*(\d{4}-\d{2}-\d{2})\s*"
    r"每\s*(10)?\s*份\s*派现金\s*([0-9]+(?:\.[0-9]+)?)\s*元"
    r"(?:\s*(\d{4}-\d{2}-\d{2}))?"
)


def parse_fhsp(html):
    """解析天天基金网“分红送配”页，返回统一为“每10份派现金”的记录。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    rows, seen = [], set()
    for m in ROW_RE.finditer(text):
        _, reg, ex, ten, amt, pay = m.groups()
        if (reg, ex) in seen:
            continue
        seen.add((reg, ex))
        value = float(amt)
        per10 = value if ten else value * 10          # “每份”换算成“每10份”
        rows.append({
            "regDate": reg,
            "exDate": ex,
            "per10": round(per10, 6),
            "payDate": pay or "",
            "raw": ("每10份" if ten else "每份") + "派现金" + amt + "元",
        })
    rows.sort(key=lambda r: r["exDate"], reverse=True)
    return rows


def fetch_fund_dividends(code):
    """返回 (是否成功取到页面, 记录列表)。"""
    url = f"https://fundf10.eastmoney.com/fhsp_{code}.html"
    headers = {
        "User-Agent": UA,
        "Referer": "https://fundf10.eastmoney.com/",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for attempt in range(1, 4):
        try:
            html = http_text(url, headers)
            if "分红送配" not in html:
                print(f"{code}: 第 {attempt} 次返回的不是分红页面（长度 {len(html)}）")
            else:
                rows = parse_fhsp(html)
                if rows:
                    r = rows[0]
                    print(f"{code}: 解析到 {len(rows)} 条分红，最近一次 除息日 {r['exDate']}，{r['raw']}")
                elif "暂无分红" in html or "暂无分红信息" in html:
                    print(f"{code}: 页面显示暂无分红")
                else:
                    print(f"{code}: 页面正常但没有解析到分红行，可能是页面结构变了")
                    return False, []
                return True, rows
        except Exception as e:
            print(f"{code}: 第 {attempt} 次失败：{e}")
        time.sleep(5)
    return False, []


def sync_fund_dividends(gist):
    codes = fund_codes_from_state(gist)
    print("需要同步分红的基金：", ", ".join(codes))

    old = {}
    try:
        txt = gist_file_text(gist, DIV_FILE)
        old = (json.loads(txt) if txt else {}).get("funds", {})
    except Exception as e:
        print("读取旧分红数据失败（首次运行可忽略）：", e)

    funds, failed = {}, []
    for code in codes:
        ok, rows = fetch_fund_dividends(code)
        if ok:
            funds[code] = rows
        else:
            failed.append(code)
            if code in old:
                funds[code] = old[code]
                print(f"{code}: 本次获取失败，沿用旧数据")

    if funds != old:
        write_gist_file(DIV_FILE, {
            "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
            "source": "fundf10.eastmoney.com",
            "funds": funds,   # { code: [{regDate, exDate, per10, payDate, raw}, ...] }
        })
        print("已写入 Gist：", DIV_FILE)
    else:
        print("分红数据没有变化，不写入")

    if failed:
        print(f"::warning::基金分红获取失败：{', '.join(failed)}（页面会沿用上次的数据）")
    return not failed, funds


def apply_dividends_to_state(funds):
    """把已到除息日的基金分红写进云备份的流水记录。
    记录格式与页面自己生成的“自动分红”完全一致（source=auto，日期=除息日），
    所以新旧版本页面都不会重复记账。"""
    gist = get_gist()                      # 重新读取最新状态，缩短与页面同时写入冲突的时间窗口
    txt = gist_file_text(gist, STATE_FILE)
    if not txt:
        print("云备份里还没有页面数据，跳过写入流水")
        return False
    doc = json.loads(txt)
    state = doc["state"]
    records = state.setdefault("records", [])
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y/%m/%d")
    added = []

    for h in state.get("holdings", []):
        code = str(h.get("code", ""))
        is_fund = h.get("assetType") == "fund" or (not h.get("assetType") and code[:1] in ("1", "5"))
        rows = funds.get(code) or []
        if not is_fund or not rows:
            continue
        buys = [r for r in records if r.get("type") == "buy" and r.get("holdingId") == h.get("id")]
        for row in rows:
            ex = str(row.get("exDate") or row.get("regDate") or "").replace("-", "/")
            reg = str(row.get("regDate") or row.get("exDate") or "").replace("-", "/")
            per_share = float(row.get("per10") or 0) / 10
            if not ex or per_share <= 0 or ex > today:
                continue                                    # 还没到除息日，先不计入
            held = sum(b.get("qty", 0) for b in buys if str(b.get("date", "")).replace("-", "/") <= reg)
            if held <= 0:
                continue                                    # 权益登记日时还没持有
            if any(r.get("type") == "dividend" and r.get("holdingId") == h.get("id")
                   and str(r.get("date", "")).replace("-", "/") == ex for r in records):
                continue                                    # 这一天已经记过
            rid = int(state.get("nextId", 100))
            state["nextId"] = rid + 1
            amount = int(held * per_share * 100 + 0.5) / 100
            records.append({"id": rid, "type": "dividend", "holdingId": h.get("id"), "date": ex,
                            "amount": amount, "source": "auto", "perShare": per_share})
            added.append(f"{code} 除息日 {ex}：{held} 份 × {per_share:g} = {amount:.2f} 元")

    if not added:
        print("云备份里的分红流水已是最新")
        return True
    doc["updatedAt"] = datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    write_gist_file(STATE_FILE, doc, indent=2)
    for line in added:
        print("已写入分红流水：", line)
    return True


# ---------------- 主流程 ----------------
def main():
    if not GIST_TOKEN or not GIST_ID:
        print("缺少 GIST_TOKEN / GIST_ID")
        sys.exit(1)
    gist = get_gist()

    index_ok = True
    try:
        index_ok = sync_indices(gist)
    except Exception as e:
        index_ok = False
        print("指数同步出错：", e)

    try:
        _, funds = sync_fund_dividends(gist)
        if funds:
            apply_dividends_to_state(funds)
    except Exception as e:
        # 分红同步失败只警告，不让整个任务变红，避免每天收到失败邮件
        print(f"::warning::基金分红同步出错：{e}")

    sys.exit(0 if index_ok else 1)


if __name__ == "__main__":
    main()
