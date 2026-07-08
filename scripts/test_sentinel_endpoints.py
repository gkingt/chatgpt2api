"""临时验证: 测试不同 sentinel endpoint 的可达性"""
from curl_cffi import requests as r

s = r.Session(impersonate="chrome", verify=False)

urls = [
    "https://sentinel.openai.com/backend-api/sentinel/frame.html",
    "https://auth.openai.com/sentinel/20260124ceb8/frame.html",
    "https://sentinel.openai.com/sentinel/20260124ceb8/frame.html",
]

for url in urls:
    resp = s.get(url, timeout=10)
    server = resp.headers.get("server", "")[:20]
    print(f"{resp.status_code}  {server:20s}  {url}")

s.close()
