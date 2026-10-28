"""Read-only cloud checks. Never logs in or sends trading instructions."""

from __future__ import annotations

import json
import math
from importlib.metadata import version

import requests

from emtl import EMTClient, QuoteMarket
from emtl.errors import EMTError


def main() -> int:
    results: dict[str, object] = {"version": version("emtl-m"), "checks": []}
    checks: list[dict[str, object]] = []
    try:
        price = EMTClient(timeout=10).last_price("600000", QuoteMarket.SH)
        checks.append({"name": "public_quote", "status": "passed" if math.isfinite(price) else "no_price"})
    except requests.exceptions.ProxyError:
        checks.append({"name": "public_quote", "status": "blocked_by_proxy"})
    except (requests.exceptions.RequestException, EMTError, ValueError) as exc:
        checks.append({"name": "public_quote", "status": "failed", "error_type": type(exc).__name__})

    try:
        response = requests.get("https://jywg.18.cn/Login", timeout=10)
        checks.append({
            "name": "public_login_page",
            "status": "passed" if response.status_code == 200 else "failed",
            "http_status": response.status_code,
        })
    except requests.exceptions.ProxyError:
        checks.append({"name": "public_login_page", "status": "blocked_by_proxy"})
    except requests.exceptions.RequestException as exc:
        checks.append({"name": "public_login_page", "status": "failed", "error_type": type(exc).__name__})

    results["checks"] = checks
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(check["status"] == "passed" for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
