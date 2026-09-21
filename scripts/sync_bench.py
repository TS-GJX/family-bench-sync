"""
从中证指数官网拉取对比指数的日收盘点位，写入家庭持仓使用的同一个 Gist。
写入的文件：bench_index_data.json（与页面自己的备份文件互不影响）
"""
import datetime
import json
import os
import sys
import time
import urllib.request

INDICES = {
    "930950": "中证偏股型基金指数",
    "000300": "沪深300",
}
START_DATE = os.environ.get("START_DATE", "20250101")
GIST_FILE = "bench_index_data.json"
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = os.environ["GIST_ID"]


def http_json(url, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def gh_headers():
    return {
        "Authorization": "token " + GIST_TOKEN,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "family-stock-bench-sync",
    }


def fetch_index(code):
    end = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y%m%d")
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           f"?indexCode={code}&startDate={START_DATE}&endDate={end}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Referer": "https://www.csindex.com.cn/",
        "Accept": "application/json, text/plain, */*",
    }
    for attempt in range(1, 4):
        try:
            j = http_json(url, headers)
            rows = j.get("data") or []
            out = []
            for r in rows:
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


def read_existing():
    try:
        gist = http_json("https://api.github.com/gists/" + GIST_ID, gh_headers())
        f = gist.get("files", {}).get(GIST_FILE)
        if f and f.get("content"):
            return json.loads(f["content"])
    except Exception as e:
        print("读取旧数据失败（首次运行可忽略）：", e)
    return {}


def main():
    old = read_existing().get("indices", {})
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
        sys.exit(1)

    payload = {
        "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "source": "csindex.com.cn",
        "names": INDICES,
        "indices": indices,   # { code: [[YYYYMMDD, close], ...] }
    }
    body = json.dumps({"files": {GIST_FILE: {
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    }}}).encode("utf-8")
    http_json("https://api.github.com/gists/" + GIST_ID, gh_headers(), body, "PATCH")
    print("已写入 Gist：", GIST_FILE)


if __name__ == "__main__":
    main()
