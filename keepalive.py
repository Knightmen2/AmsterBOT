"""Пинг Render, чтобы бесплатный Web Service не засыпал.

Локально (ПК должен быть включён):
    set KEEP_ALIVE_URL=https://ВАШ-СЕРВИС.onrender.com
    python keepalive.py

Или один раз:
    python keepalive.py https://ВАШ-СЕРВИС.onrender.com

Интервал по умолчанию 5 минут (KEEP_ALIVE_INTERVAL_SEC).
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_INTERVAL = 300


def ping(url: str, timeout: float = 30.0) -> tuple[bool, str]:
    target = url.rstrip("/") + "/"
    request = urllib.request.Request(
        target,
        method="GET",
        headers={"User-Agent": "bcwallfiles-keepalive/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = response.getcode()
            body = response.read(64).decode("utf-8", errors="replace")
            return 200 <= code < 400, f"HTTP {code} {body!r}"
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("KEEP_ALIVE_URL", "")).strip()
    if not url:
        print("Задайте URL: KEEP_ALIVE_URL или аргумент python keepalive.py https://....onrender.com")
        return 1

    interval = int(os.getenv("KEEP_ALIVE_INTERVAL_SEC", str(DEFAULT_INTERVAL)))
    if interval < 60:
        interval = 60

    print(f"Пинг {url} каждые {interval} сек. Ctrl+C — стоп.")
    while True:
        ok, detail = ping(url)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {'OK' if ok else 'FAIL'}: {detail}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
        raise SystemExit(0)
