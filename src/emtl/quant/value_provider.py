"""Observed-today public annual financials; never backdated to the report date."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .data import EastmoneyDailyProvider
from .models import Instrument
from .value import Fundamental


class EastmoneyValueProvider:
    def __init__(self) -> None:
        self.session = EastmoneyDailyProvider().session

    def capture(self, instruments: tuple[Instrument, ...]) -> tuple[list[Fundamental], dict]:
        observed = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
        if not instruments:
            raise ValueError("candidate stocks must be nonempty")
        result, evidence = (
            [],
            {"observed_at": observed.isoformat(), "profiles": {}, "annual_reports": {}},
        )
        columns = "SECUCODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,UPDATE_DATE,EPSJB,BPS,MGJYXJJE,ROEJQ,ZCFZL,ORG_TYPE"
        for instrument in instruments:
            symbol = instrument.symbol
            response = self.session.get(
                "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                params={
                    "reportName": "RPT_F10_BASIC_ORGINFO",
                    "columns": "SECUCODE,SECURITY_NAME_ABBR,LISTING_DATE,EM2016,INDUSTRYCSRC1,SECURITY_TYPE_CODE",
                    "filter": f'(SECUCODE="{symbol}")',
                    "pageSize": 1,
                    "pageNumber": 1,
                },
                timeout=20,
            )
            response.raise_for_status()
            profile_payload = response.json()
            profiles = (profile_payload.get("result") or {}).get("data")
            if (
                not isinstance(profiles, list)
                or len(profiles) != 1
                or profiles[0].get("SECUCODE") != symbol
            ):
                raise ValueError(f"issuer profile missing for {symbol}; complete capture aborted")
            profile = profiles[0]
            evidence["profiles"][symbol] = profile_payload
            response = self.session.get(
                "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                params={
                    "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                    "columns": columns,
                    "filter": f'(SECUCODE="{symbol}")(REPORT_TYPE="年报")',
                    "pageSize": 5,
                    "pageNumber": 1,
                    "sortColumns": "REPORT_DATE",
                    "sortTypes": "-1",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            report = payload.get("result") if isinstance(payload, dict) else None
            annuals = report.get("data") if isinstance(report, dict) else None
            if not isinstance(annuals, list):
                raise ValueError(f"annual data missing for {symbol}; complete capture aborted")
            evidence["annual_reports"][symbol] = payload
            available = [
                r for r in annuals if str(r.get("NOTICE_DATE", "9999"))[:10] < str(observed.date())
            ]
            if not available:
                raise ValueError(f"no published annual report for {symbol}")
            annual = max(available, key=lambda r: r["REPORT_DATE"])
            if annual.get("SECUCODE") != symbol:
                raise ValueError(f"annual-report symbol mismatch for {symbol}")
            industry = profile.get("EM2016") or profile.get("INDUSTRYCSRC1")
            if annual.get("ORG_TYPE") in {"银行", "保险", "证券"}:
                industry = annual["ORG_TYPE"]
            if not isinstance(industry, str) or industry in {"", "-"}:
                raise ValueError(f"industry metadata missing for {symbol}")
            name = str(profile["SECURITY_NAME_ABBR"])

            def number(key: str, *, percent: bool = False, annual: dict = annual) -> float | None:
                value = annual.get(key)
                return (
                    float(value) / (100 if percent else 1) if value not in {None, "", "-"} else None
                )

            publication = dt.date.fromisoformat(annual["NOTICE_DATE"][:10])
            result.append(
                Fundamental(
                    symbol=symbol,
                    period_end=dt.date.fromisoformat(annual["REPORT_DATE"][:10]),
                    published_on=publication,
                    known_on=max(observed.date(), publication + dt.timedelta(days=1)),
                    listed_on=dt.date.fromisoformat(profile["LISTING_DATE"][:10]),
                    name=name,
                    industry=industry,
                    annual_eps=number("EPSJB"),
                    book_value_per_share=number("BPS"),
                    roe=number("ROEJQ", percent=True),
                    operating_cash_flow_per_share=number("MGJYXJJE"),
                    debt_ratio=number("ZCFZL", percent=True),
                    is_st="ST" in name.upper() or "退" in name,
                    is_active="退" not in name,
                    source="eastmoney_observed_annual",
                )
            )
        return result, evidence
