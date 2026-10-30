"""Neocloud financing tracker assembled from public SEC EDGAR filings.

Listed issuers contribute balance-sheet and cash-flow facts from their XBRL
company facts plus financing events parsed from 8-K, 6-K and prospectus
filings. Private issuers contribute Form D exempt-offering notices and IPO
registrations. Every observation is persisted so restarts and SEC outages keep
serving the accumulated ledger.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, cast

import httpx

from app import db

logger = logging.getLogger(__name__)

SEC_DATA_URL = "https://data.sec.gov"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_SEARCH_URL = "https://www.sec.gov/edgar/search/"
DEFAULT_USER_AGENT = "SectorTracker/0.1 (+AI market dashboard) contact@example.com"
CACHE_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 30.0
# SEC fair-access policy caps automated clients at 10 requests per second.
REQUEST_INTERVAL_SECONDS = 0.15
# Three years spans the GPU-cloud build-out financings and older Form D rounds.
EVENT_LOOKBACK_DAYS = 1095
# Prospectus supplements run to megabytes; the cover page is all we parse.
DOCUMENT_BYTE_LIMIT = 400_000
EVENT_MERGE_DAYS = 10
EVENT_LIMIT = 250
SERIES_QUARTERS = 8
HISTORY_QUARTERS = 12
# A balance an issuer stopped tagging stays visible, dated, for up to a year.
INSTANT_FALLBACK_DAYS = 400
# Smaller figures in deal text are par values, share prices or fees.
MIN_DEAL_AMOUNT = 1_000_000
# Bump when filing parsing changes so stored filings are reviewed again.
PARSER_VERSION = 3
# Collateral and security language sits in the first pages of the terms.
DEBT_TERMS_CHARS = 6000

_PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F")
_PERIODIC_FILINGS = {"10-K", "10-KT", "10-Q", "20-F", "40-F"}
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_LINKBASE_RE = re.compile(r"_(?:cal|def|lab|pre)\.xml$|FilingSummary\.xml$|MetaLinks", re.I)
# Principal-maturity buckets as offsets from the fiscal year that contains the
# balance-sheet date; "after year N" buckets are open-ended.
_SCHEDULE_RE = re.compile(
    r"^LongTermDebtMaturit\w*?(?P<bucket>RemainderOfFiscalYear|InNextRollingTwelveMonths"
    r"|InNextTwelveMonths|InYearOne|InRollingYear(?:Two|Three|Four|Five)"
    r"|InYear(?:Two|Three|Four|Five)|AfterRollingYear(?:Four|Five)|AfterYear(?:Four|Five))$"
)
_SCHEDULE_OFFSETS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
_SCHEDULE_AXES = ("LongtermDebtTypeAxis", "DebtInstrumentAxis")
# Debt maturity wall segments, from most to least authoritative.
WALL_SEGMENTS = ("reported", "reported_split", "after_report", "deals", "deals_amortizing")


@dataclass(frozen=True)
class NeocloudCompany:
    key: str
    name: str
    segment: str
    cik: int
    listed: bool


NEOCLOUD_COMPANIES = (
    NeocloudCompany("CRWV", "CoreWeave", "GPU cloud", 1769628, True),
    NeocloudCompany("NBIS", "Nebius", "GPU cloud", 1513845, True),
    NeocloudCompany("IREN", "IREN", "GPU cloud", 1878848, True),
    NeocloudCompany("WYFI", "WhiteFiber", "GPU cloud", 2042022, True),
    NeocloudCompany("APLD", "Applied Digital", "AI data centers", 1144879, True),
    NeocloudCompany("CIFR", "Cipher Digital", "AI data centers", 1819989, True),
    NeocloudCompany("WULF", "TeraWulf", "AI data centers", 1083301, True),
    NeocloudCompany("CORZ", "Core Scientific", "AI data centers", 1839341, True),
    NeocloudCompany("HUT", "Hut 8", "AI data centers", 1964789, True),
    NeocloudCompany("CRUSOE", "Crusoe", "GPU cloud", 1924674, False),
    NeocloudCompany("LAMBDA", "Lambda", "GPU cloud", 1954966, False),
    NeocloudCompany("FLUIDSTACK", "Fluidstack", "GPU cloud", 2104151, False),
    NeocloudCompany("NSCALE", "Nscale", "GPU cloud", 2110365, False),
    NeocloudCompany("TENSORWAVE", "TensorWave", "GPU cloud", 2066342, False),
    NeocloudCompany("LANCIUM", "Lancium", "AI data centers", 1886863, False),
)


# Duration lines in preference order; per reporting period the first tagged
# concept wins, because issuers switch tags across filings. Gross debt and
# equity proceeds are not tracked here: issuers book them under custom
# extension tags missing from company facts, so the deal ledger covers them.
_FLOW_METRICS: dict[str, tuple[tuple[str, ...], bool]] = {
    "revenue": (
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        False,
    ),
    "capex": (
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquireMachineryAndEquipment",
        ),
        False,
    ),
    "interest": (
        ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
        False,
    ),
    "net_financing": (
        (
            "NetCashProvidedByUsedInFinancingActivities",
            "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
        ),
        True,
    ),
}

# Balance-sheet lines: the first option whose leading concept is reported wins,
# plus whichever of its companion concepts are reported (e.g. current debt).
_INSTANT_METRICS: dict[str, tuple[tuple[str, ...], ...]] = {
    "cash": (
        ("CashAndCashEquivalentsAtCarryingValue",),
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",),
    ),
    "debt": (
        ("LongTermDebt",),
        ("DebtLongtermAndShorttermCombinedAmount",),
        ("LongTermDebtNoncurrent", "LongTermDebtCurrent"),
        ("LongTermNotesPayable", "NotesPayableCurrent"),
        ("DebtInstrumentCarryingAmount",),
    ),
    "leases": (
        ("OperatingLeaseLiability", "FinanceLeaseLiability"),
        ("FinanceLeaseLiability",),
    ),
    "backlog": (("RevenueRemainingPerformanceObligation",),),
}

KIND_CATEGORY = {
    "Loan facility": "debt",
    "Senior notes": "debt",
    "Convertible notes": "debt",
    "Private debt": "debt",
    "Equity offering": "equity",
    "ATM program": "equity",
    "Private round": "equity",
    "Private placement": "equity",
    "IPO filing": "equity",
    "Contract": "contract",
    "Lease": "contract",
}

_EVENT_ITEMS = {"1.01", "2.03", "3.02"}
_PROSPECTUS_FORMS = {"424B2", "424B4", "424B5"}
_MONEY_RE = re.compile(
    r"(US\$|A\$|C\$|\$|€|£)\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(billion|million|thousand)\b)?",
    re.IGNORECASE,
)
_CURRENCIES = {"$": "USD", "US$": "USD", "A$": "AUD", "C$": "CAD", "€": "EUR", "£": "GBP"}
_SCALES = {"billion": 1e9, "million": 1e6, "thousand": 1e3}
_ITEM_RE = re.compile(r"Item\s*(\d\.\d\d)[\s.:]", re.IGNORECASE)
_ITEM_TITLE_RE = re.compile(
    r"^Item\s*\d\.\d\d\.?\s*(?:Entry into (?:or Amendment of )?a Material Definitive Agreement"
    r"|Creation of a Direct Financial Obligation.*?Registrant"
    r"|Unregistered Sales of Equity Securities)\.?\s*",
    re.IGNORECASE,
)
_MERGER_RE = re.compile(
    r"agreement and plan of merger|merger agreement|acquisition of", re.IGNORECASE
)
_ATM_RE = re.compile(
    r"equity distribution agreement|at[- ]the[- ]market|sales agreement|controlled equity",
    re.IGNORECASE,
)
_NOTES_RE = re.compile(r"senior (?:secured )?notes|notes due|indenture", re.IGNORECASE)
_LOAN_RE = re.compile(
    r"delayed[- ]draw|term loan|credit agreement|credit facility|loan agreement|loan facility"
    r"|revolving|asset[- ]backed facility|bridge facility|financing agreement|promissory note",
    re.IGNORECASE,
)
_CONVERTIBLE_RE = re.compile(
    r"convertible (?:senior )?(?:secured )?(?:notes|debentures)", re.IGNORECASE
)
_EQUITY_RE = re.compile(
    r"underwriting agreement|securities purchase agreement|subscription agreement"
    r"|private placement|pre-funded warrant|preferred (?:stock|shares)",
    re.IGNORECASE,
)
_SHARES_RE = re.compile(r"common stock|ordinary shares|class a|\bshares\b", re.IGNORECASE)
_CONTRACT_6K_RE = re.compile(
    r"commercial agreement|infrastructure service agreement|master services agreement",
    re.IGNORECASE,
)
_LEASE_RE = re.compile(r"\bleases?\b", re.IGNORECASE)
_PROSPECTUS_BOILERPLATE_RE = re.compile(
    r"PROSPECTUS SUPPLEMENT|TABLE OF CONTENTS|Filed pursuant to Rule 424\(b\)\(\d\)"
    r"|\(to prospectus (?:supplement )?dated [^)]*\)|Registration (?:Statement )?No\. ?[\d-]+",
    re.IGNORECASE,
)

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_ORDINALS = {
    word: index
    for index, words in enumerate(
        (
            ("first", "one"), ("second", "two"), ("third", "three"), ("fourth", "four"),
            ("fifth", "five"), ("sixth", "six"), ("seventh", "seven"), ("eighth", "eight"),
            ("ninth", "nine"), ("tenth", "ten"),
        ),
        start=1,
    )
    for word in words
}
_ORDINAL_WORDS = "|".join(_ORDINALS)
# A maturity keyword followed within the clause by a full date; the window may
# cross periods in names such as "DDTL 5.5 Facility" but not a semicolon.
_MATURITY_DATE_RE = re.compile(
    r"(?:matur\w*|due and payable|final (?:scheduled )?(?:payment|repayment) date)[^;]{0,160}?"
    r"\b(?P<month>" + "|".join(_MONTHS) + r")\s+(?P<day>\d{1,2}),\s*(?P<year>20\d\d)",
    re.IGNORECASE,
)
_ANNIVERSARY_RE = re.compile(
    r"(?:matur\w*|until|terminat\w*)[^;]{0,160}?\b(" + _ORDINAL_WORDS + r")\s+anniversary of the"
    r" (?:closing|effective|issue|funding|initial borrowing) date",
    re.IGNORECASE,
)
# "will mature, and all obligations thereunder will become due and payable, on
# the date that is 364 days after the Closing Date".
_DAYS_AFTER_RE = re.compile(r"matur\w*[^;]{0,120}?\b(\d{2,4}) days after", re.IGNORECASE)
_YEARS_AFTER_RE = re.compile(
    r"matur\w*[^;]{0,120}?\b(\d{1,2}|" + _ORDINAL_WORDS + r") years after", re.IGNORECASE
)
_DUE_YEAR_RE = re.compile(r"(?:Notes|Debentures|Loans?) due (20\d\d)")
_SECURED_RE = re.compile(
    r"senior secured|secured by|first[- ]priority (?:lien|security interest)|collateralized by"
    r"|security interest in|secured on a",
    re.IGNORECASE,
)
_SECURED_CONVERTIBLE_RE = re.compile(
    r"convertible senior secured|secured convertible", re.IGNORECASE
)
_UNSECURED_RE = re.compile(r"\bunsecured\b", re.IGNORECASE)
_AMENDMENT_RE = re.compile(r"\bamend(?:ment|ed)\b", re.IGNORECASE)
_PERCENT = r"(\d{1,2}(?:\.\d{1,4})?)%"
# "9.625% Senior Secured Notes", "0.00% Convertible Senior Notes".
_COUPON_RE = re.compile(
    _PERCENT + r"\s+(?:Senior|Secured|Convertible|Exchangeable|Unsecured|Notes|Debentures)\b",
    re.IGNORECASE,
)
_PER_ANNUM_RE = re.compile(
    r"(?:bears?|accrues?)\s+interest\s+at\s+(?:a\s+(?:fixed\s+)?rate\s+(?:of|equal\s+to)\s+)?"
    + _PERCENT
    + r"\s+per\s+annum",
    re.IGNORECASE,
)
_SPREAD = r"(?P<low>\d{1,2}(?:\.\d{1,3})?)%(?:\s+to\s+(?P<high>\d{1,2}(?:\.\d{1,3})?)%)?"
_FLOATING_RATE_PATTERNS = (
    # "Term SOFR (subject to a 0.00% floor) plus an applicable margin of 2.25%"
    re.compile(
        # Decimal points ("a 0.00% floor") may sit inside the clause; sentence ends may not.
        r"SOFR(?:[^.;]|\.(?=\d)){0,90}?plus\s+(?:an?\s+)?(?:applicable\s+)?(?:margin|spread)\s+"
        r"(?:of\s+|equal\s+to\s+)?(?:ranging\s+from\s+)?" + _SPREAD,
        re.IGNORECASE,
    ),
    # "4.00% plus the daily compounded SOFR"
    re.compile(
        _SPREAD + r"\s+plus\s+(?:the\s+)?(?:daily\s+(?:compounded|simple)\s+|term\s+)?SOFR",
        re.IGNORECASE,
    ),
    # "The applicable margin will be 2.25%, in the case of Term SOFR-based loans"
    re.compile(
        r"applicable\s+margin\s+will\s+be\s+" + _SPREAD + r",?\s+in\s+the\s+case\s+of\s+"
        r"(?:Term\s+)?SOFR",
        re.IGNORECASE,
    ),
)
_AMORTIZATION_RE = re.compile(
    r"amortiz\w*[^.;]{0,160}?beginning\s+(?:on|with)\s+(?:the\s+)?(?P<month>"
    + "|".join(_MONTHS)
    + r")\s+(?P<day>\d{1,2}),\s*(?P<year>20\d\d)",
    re.IGNORECASE,
)
_PAYMENT_FREQUENCIES = (
    ("semi-annual", 2),
    ("semiannual", 2),
    ("quarterly", 4),
    ("monthly", 12),
    ("annual", 1),
)
_COLLATERAL_PATTERNS = (
    (
        "GPUs",
        re.compile(
            r"\bGPUs?\b|graphics processing|compute equipment|computing equipment|\bservers\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Data centers",
        re.compile(r"data cent(?:er|re)s?|real property|\bcampus\b|leasehold", re.IGNORECASE),
    ),
    (
        "Contracts",
        re.compile(
            r"customer contracts?|master services agreements?|receivables"
            r"|contracted (?:cash flows|revenue)",
            re.IGNORECASE,
        ),
    ),
    (
        "Equity pledge",
        re.compile(
            r"pledge of (?:the |all )?(?:equity|membership|capital stock)"
            r"|equity interests? (?:in|of) the (?:borrower|issuer)",
            re.IGNORECASE,
        ),
    ),
)


class NeocloudFinanceError(RuntimeError):
    """No current or persisted neocloud financing observations are available."""


class NeocloudFinanceService:
    """Sync SEC filings for tracked neoclouds and serve the composed ledger."""

    def __init__(
        self,
        database_path: Path,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_seconds: float = CACHE_SECONDS,
        request_interval: float = REQUEST_INTERVAL_SECONDS,
        client: httpx.AsyncClient | None = None,
        companies: tuple[NeocloudCompany, ...] = NEOCLOUD_COMPANIES,
        today: date | None = None,
    ) -> None:
        self.database_path = database_path
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self.cache_seconds = cache_seconds
        self.request_interval = request_interval
        self.companies = companies
        self._today = today
        self._client = client
        self._owns_client = client is None
        self._cache: tuple[float, dict[str, object]] | None = None
        self._lock = asyncio.Lock()
        self._throttle = asyncio.Lock()
        self._next_request_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def get_financing(self) -> dict[str, object]:
        now = monotonic()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        async with self._lock:
            now = monotonic()
            if self._cache is not None and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]

            today = self._today or datetime.now(UTC).date()
            cutoff = today - timedelta(days=EVENT_LOOKBACK_DAYS)
            known = await asyncio.to_thread(
                db.load_neocloud_filing_accessions,
                self.database_path,
                parser_version=PARSER_VERSION,
            )
            stored_schedules = await asyncio.to_thread(
                db.load_neocloud_debt_schedules, self.database_path
            )
            schedule_accessions = {
                key: str(schedule["accession"]) for key, schedule in stored_schedules.items()
            }
            results = await asyncio.gather(
                *(
                    self._sync_company(company, known, cutoff, schedule_accessions)
                    for company in self.companies
                ),
                return_exceptions=True,
            )
            financial_rows: list[dict[str, object]] = []
            filing_rows: list[dict[str, object]] = []
            for company, result in zip(self.companies, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.warning("SEC sync failed for %s: %s", company.key, result)
                    continue
                financial_rows.extend(result[0])
                filing_rows.extend(result[1])
                if result[2] is not None:
                    await asyncio.to_thread(
                        db.save_neocloud_debt_schedule, self.database_path, result[2]
                    )

            if financial_rows:
                await asyncio.to_thread(
                    db.save_neocloud_financials, self.database_path, financial_rows
                )
            if filing_rows:
                await asyncio.to_thread(
                    db.save_neocloud_filings,
                    self.database_path,
                    filing_rows,
                    parser_version=PARSER_VERSION,
                )
            financials = await asyncio.to_thread(db.load_neocloud_financials, self.database_path)
            events = await asyncio.to_thread(db.load_neocloud_events, self.database_path)
            schedules = await asyncio.to_thread(db.load_neocloud_debt_schedules, self.database_path)
            if not financials and not events:
                if self._cache is not None:
                    return self._cache[1]
                raise NeocloudFinanceError("neocloud_finance_unavailable")

            payload = compose_payload(
                self.companies, financials, events, schedules, today=today
            )
            self._cache = (monotonic(), payload)
            return payload

    async def _sync_company(
        self,
        company: NeocloudCompany,
        known: set[str],
        cutoff: date,
        schedule_accessions: dict[str, str],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object] | None]:
        financial_rows: list[dict[str, object]] = []
        if company.listed:
            facts = await self._get_json(
                f"{SEC_DATA_URL}/api/xbrl/companyfacts/CIK{company.cik:010d}.json"
            )
            financial_rows = [
                {"company": company.key, **row} for row in parse_company_facts(facts)
            ]

        submissions = await self._get_json(
            f"{SEC_DATA_URL}/submissions/CIK{company.cik:010d}.json"
        )
        filings = _filing_index(submissions.get("filings", {}).get("recent", {}))
        for page in submissions.get("filings", {}).get("files", []) or []:
            if str(page.get("filingTo") or "") >= cutoff.isoformat() and page.get("name"):
                older = await self._get_json(f"{SEC_DATA_URL}/submissions/{page['name']}")
                filings.extend(_filing_index(older))

        filing_rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for filing in filings:
            accession = filing["accession"]
            if accession in known or accession in seen or filing["filed"] < cutoff.isoformat():
                continue
            if not _is_candidate(filing, company.listed):
                continue
            seen.add(accession)
            try:
                row = await self._parse_filing(company, filing)
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                # Not persisted, so the next refresh retries the document.
                logger.warning("SEC filing %s for %s skipped: %s", accession, company.key, exc)
                continue
            filing_rows.append(row)
        schedule: dict[str, object] | None = None
        periodic = next((filing for filing in filings if filing["form"] in _PERIODIC_FILINGS), None)
        if (
            company.listed
            and periodic is not None
            and schedule_accessions.get(company.key) != periodic["accession"]
        ):
            try:
                schedule = await self._fetch_schedule(company, periodic)
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                logger.warning("SEC debt schedule for %s skipped: %s", company.key, exc)
        return financial_rows, filing_rows, schedule

    async def _fetch_schedule(
        self,
        company: NeocloudCompany,
        filing: dict[str, str],
    ) -> dict[str, object]:
        """Read the principal maturity schedule from the filing's XBRL instance."""
        folder = f"{SEC_ARCHIVES_URL}/{company.cik}/{filing['accession'].replace('-', '')}"
        index = await self._get_json(f"{folder}/index.json")
        items = index.get("directory", {}).get("item", []) or []
        names = [str(item.get("name", "")) for item in items if isinstance(item, dict)]
        instance = next((name for name in names if name.endswith("_htm.xml")), None) or next(
            (
                name
                for name in names
                if name.endswith(".xml")
                and not _LINKBASE_RE.search(name)
                and name != "primary_doc.xml"
            ),
            None,
        )
        parsed: dict[str, object] = {"as_of": None, "fiscal_year_end": None, "buckets": []}
        if instance is not None:
            parsed = parse_reported_schedule(await self._get_bytes(f"{folder}/{instance}"))
        return {
            "company": company.key,
            "accession": filing["accession"],
            "form": filing["form"],
            "url": f"{folder}/{filing['document']}",
            **parsed,
        }

    async def _parse_filing(
        self,
        company: NeocloudCompany,
        filing: dict[str, str],
    ) -> dict[str, object]:
        form = filing["form"]
        folder = f"{SEC_ARCHIVES_URL}/{company.cik}/{filing['accession'].replace('-', '')}"
        document = filing["document"]
        url = f"{folder}/{document}" if document else f"{folder}/"
        row: dict[str, object] = {
            "accession": filing["accession"],
            "company": company.key,
            "form": form,
            "filed_date": filing["filed"],
            "url": url,
            "kind": None,
            "amount": None,
            "currency": None,
            "offering_amount": None,
            "first_sale_date": None,
            "summary": "",
            "maturity": None,
            "secured": None,
            "collateral": [],
            "amendment": False,
        }
        if form in {"S-1", "F-1"}:
            row.update(
                kind="IPO filing",
                summary=f"{company.name} filed a {form} registration statement for an IPO.",
            )
            return row
        if form in {"D", "D/A"}:
            xml_name = document.rsplit("/", 1)[-1] or "primary_doc.xml"
            notice = parse_form_d(await self._get_text(f"{folder}/{xml_name}"))
            if notice is not None:
                row.update(notice)
            return row
        if not document:
            return row
        document_text = await self._get_text(url)
        event = parse_filing_event(
            form, filing["items"], document_text, filed=date.fromisoformat(filing["filed"])
        )
        if event is not None:
            row.update(event)
        return row

    async def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
                follow_redirects=True,
            )
        return self._client

    async def _pace(self) -> None:
        async with self._throttle:
            wait = self._next_request_at - monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = monotonic() + self.request_interval

    async def _get_json(self, url: str) -> dict[str, Any]:
        client = await self._client_instance()
        await self._pace()
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"unexpected SEC payload from {url}")
        return cast(dict[str, Any], payload)

    async def _get_text(self, url: str) -> str:
        content = await self._get_bytes(url, limit=DOCUMENT_BYTE_LIMIT)
        return content.decode("utf-8", errors="replace")

    async def _get_bytes(self, url: str, *, limit: int | None = None) -> bytes:
        client = await self._client_instance()
        await self._pace()
        chunks: list[bytes] = []
        size = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if limit is not None and size >= limit:
                    break
        return b"".join(chunks)


# --- Filing index -----------------------------------------------------------


def _filing_index(columns: dict[str, Any]) -> list[dict[str, str]]:
    forms = columns.get("form") or []
    fields = ("accessionNumber", "filingDate", "items", "primaryDocument")
    values = [columns.get(field) or [] for field in fields]
    filings: list[dict[str, str]] = []
    for index, form in enumerate(forms):
        accession, filed, items, document = (
            str(column[index]) if index < len(column) and column[index] is not None else ""
            for column in values
        )
        if accession and filed:
            filings.append(
                {
                    "form": str(form),
                    "accession": accession,
                    "filed": filed,
                    "items": items,
                    "document": document,
                }
            )
    return filings


def _is_candidate(filing: dict[str, str], listed: bool) -> bool:
    form = filing["form"]
    if form in {"D", "D/A"}:
        return True
    if form in {"S-1", "F-1"}:
        # Listed issuers use S-1/F-1 for resale shelves, not new capital.
        return not listed
    if form == "8-K":
        return bool(_EVENT_ITEMS & {item.strip() for item in filing["items"].split(",")})
    return form == "6-K" or form in _PROSPECTUS_FORMS


# --- XBRL company facts -----------------------------------------------------


def parse_company_facts(payload: dict[str, Any]) -> list[dict[str, object]]:
    """Derive quarterly, trailing-twelve-month and balance-sheet rows."""
    concepts = cast(dict[str, Any], payload.get("facts", {}).get("us-gaap", {}))

    def usd_facts(concept: str) -> list[dict[str, Any]]:
        units = concepts.get(concept, {}).get("units", {})
        return [
            fact
            for fact in units.get("USD", [])
            if str(fact.get("form", "")).startswith(_PERIODIC_FORMS)
            and _number(fact.get("val")) is not None
        ]

    rows: list[dict[str, object]] = []
    for metric, (preferred, signed) in _FLOW_METRICS.items():
        durations: dict[tuple[str, str], float] = {}
        # Merge before deriving quarters: an issuer may tag its fiscal year and
        # its year-to-date periods under different concepts.
        for concept in reversed(preferred):
            durations.update(_latest_durations(usd_facts(concept)))
        quarter_values, annual_values = _flow_periods(durations, signed=signed)
        quarter_ends = sorted(quarter_values)[-HISTORY_QUARTERS:]
        for end in quarter_ends:
            rows.append({"period_end": end, "metric": f"{metric}_q", "value": quarter_values[end]})
        for end in sorted({*quarter_ends, *annual_values})[-HISTORY_QUARTERS:]:
            ttm = _trailing_twelve_months(end, quarter_values, annual_values)
            if ttm is not None:
                rows.append({"period_end": end, "metric": f"{metric}_ttm", "value": ttm})

    for metric, options in _INSTANT_METRICS.items():
        by_concept = {
            concept: _instant_periods(usd_facts(concept))
            for option in options
            for concept in option
        }
        ends = sorted({end for values in by_concept.values() for end in values})
        for end in ends[-HISTORY_QUARTERS:]:
            option = next((choice for choice in options if end in by_concept[choice[0]]), None)
            if option is not None:
                value = sum(by_concept[name][end] for name in option if end in by_concept[name])
                rows.append({"period_end": end, "metric": metric, "value": value})
    return rows


def _latest_durations(facts: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        start, end = fact.get("start"), fact.get("end")
        if not start or not end:
            continue
        key = (str(start), str(end))
        if key not in latest or str(fact.get("filed", "")) > str(latest[key].get("filed", "")):
            latest[key] = fact
    return {key: float(fact["val"]) for key, fact in latest.items()}


def _flow_periods(
    durations: dict[tuple[str, str], float],
    *,
    signed: bool,
) -> tuple[dict[str, float], dict[str, float]]:
    """Split duration values into discrete quarters and fiscal years.

    10-Q cash-flow lines are year-to-date, so a quarter is the difference
    between consecutive cumulative periods sharing a start date. A negative
    difference on an unsigned line exposes a restatement or tag switch, so
    that quarter is left unknown rather than invented.
    """
    quarters: dict[str, float] = {}
    years: dict[str, float] = {}
    chains: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (start, end), value in durations.items():
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if 80 <= days <= 100:
            quarters[end] = value
        elif 350 <= days <= 380:
            years[end] = value
        chains[start].append((end, value))
    for chain in chains.values():
        chain.sort()
        for (previous_end, previous_value), (end, value) in zip(chain, chain[1:], strict=False):
            gap = (date.fromisoformat(end) - date.fromisoformat(previous_end)).days
            difference = value - previous_value
            if 80 <= gap <= 100 and end not in quarters and (signed or difference >= 0):
                quarters[end] = difference
    return quarters, years


def _instant_periods(facts: list[dict[str, Any]]) -> dict[str, float]:
    latest: dict[str, dict[str, Any]] = {}
    for fact in facts:
        end = fact.get("end")
        if fact.get("start") or not end:
            continue
        if end not in latest or str(fact.get("filed", "")) > str(latest[end].get("filed", "")):
            latest[str(end)] = fact
    return {end: float(fact["val"]) for end, fact in latest.items()}


def _trailing_twelve_months(
    end: str,
    quarters: dict[str, float],
    years: dict[str, float],
) -> float | None:
    if end in years:
        return years[end]
    ends = [quarter_end for quarter_end in sorted(quarters) if quarter_end <= end]
    if len(ends) < 4 or ends[-1] != end:
        return None
    window = ends[-4:]
    span = (date.fromisoformat(window[-1]) - date.fromisoformat(window[0])).days
    if not 250 <= span <= 300:
        return None
    return sum(quarters[quarter_end] for quarter_end in window)


# --- Filing documents -------------------------------------------------------


def parse_reported_schedule(instance: bytes) -> dict[str, object]:
    """Principal maturity schedule tagged in a 10-Q/10-K XBRL instance.

    Uses the undimensioned schedule when the issuer tags one; otherwise sums the
    schedule tagged per debt type (or per instrument). Buckets are mapped to
    fiscal-year end years: the remainder of the current fiscal year is offset 0,
    "year one" the next fiscal year, and "after year N" stays open-ended.
    """
    root = ET.fromstring(instance)
    contexts: dict[str, tuple[dict[str, str], str]] = {}
    for context in root.iter(f"{_XBRLI}context"):
        dims = {
            str(member.get("dimension", "")).split(":")[-1]: (member.text or "").strip()
            for member in context.iter(f"{_XBRLDI}explicitMember")
        }
        period = context.find(f"{_XBRLI}period")
        when = ""
        if period is not None:
            when = (
                period.findtext(f"{_XBRLI}instant") or period.findtext(f"{_XBRLI}endDate") or ""
            ).strip()
        contexts[str(context.get("id"))] = (dims, when)
    units = {
        str(unit.get("id")): " ".join(
            (measure.text or "") for measure in unit.iter(f"{_XBRLI}measure")
        )
        for unit in root.iter(f"{_XBRLI}unit")
    }
    as_of: str | None = None
    fiscal_year_end: str | None = None
    tagged: list[tuple[str, dict[str, str], str, float]] = []
    for element in root:
        name = element.tag.rsplit("}", 1)[-1]
        if name == "DocumentPeriodEndDate" and element.text:
            as_of = element.text.strip()
        elif name == "CurrentFiscalYearEndDate" and element.text:
            fiscal_year_end = element.text.strip()
        match = _SCHEDULE_RE.match(name)
        if match is None or "Interest" in name or not element.text:
            continue
        if units.get(str(element.get("unitRef")), "") != "iso4217:USD":
            continue
        dims, when = contexts.get(str(element.get("contextRef")), ({}, ""))
        value = _number(element.text.strip())
        if value is not None:
            tagged.append((match.group("bucket"), dims, when, value))
    try:
        balance_date = date.fromisoformat(str(as_of))
    except ValueError:
        return {"as_of": None, "fiscal_year_end": fiscal_year_end, "buckets": []}

    groups: dict[tuple[str, str], dict[tuple[int, bool], float]] = defaultdict(dict)
    for bucket, dims, when, value in tagged:
        if when != balance_date.isoformat():
            continue
        if not dims:
            group = ("", "")
        elif len(dims) == 1 and next(iter(dims)) in _SCHEDULE_AXES:
            axis, member = next(iter(dims.items()))
            group = (axis, member)
        else:
            continue
        groups[group].setdefault(_schedule_offset(bucket), value)
    if ("", "") in groups:
        chosen = [groups[("", "")]]
    else:
        chosen_axis = next(
            (name for name in _SCHEDULE_AXES if any(group[0] == name for group in groups)), None
        )
        chosen = [values for (axis_name, _), values in groups.items() if axis_name == chosen_axis]
    totals: dict[tuple[int, bool], float] = defaultdict(float)
    for values in chosen:
        for key, value in values.items():
            totals[key] += value
    base_year = _fiscal_base_year(balance_date, fiscal_year_end)
    return {
        "as_of": balance_date.isoformat(),
        "fiscal_year_end": fiscal_year_end,
        "buckets": [
            {"offset": offset, "year": base_year + offset, "open": is_open, "amount": amount}
            for (offset, is_open), amount in sorted(totals.items())
            if amount
        ],
    }


def _schedule_offset(bucket: str) -> tuple[int, bool]:
    if bucket == "RemainderOfFiscalYear":
        return 0, False
    if bucket in {"InNextTwelveMonths", "InNextRollingTwelveMonths", "InYearOne"}:
        return 1, False
    year = _SCHEDULE_OFFSETS[re.sub(r"^(?:In|After)(?:Rolling)?Year", "", bucket)]
    # "After year four" starts in fiscal year five.
    return (year + 1, True) if bucket.startswith("After") else (year, False)


def _fiscal_base_year(balance_date: date, fiscal_year_end: str | None) -> int:
    """Year in which the fiscal year containing the balance-sheet date ends."""
    year_end = _fiscal_year_end_date(balance_date.year, fiscal_year_end)
    return balance_date.year if year_end >= balance_date else balance_date.year + 1


def _fiscal_year_end_date(year: int, fiscal_year_end: object) -> date:
    """Last day of the fiscal year ending in `year` ("--06-30" style, default December)."""
    match = re.fullmatch(r"--(\d{2})-(\d{2})", str(fiscal_year_end or ""))
    month, day = (int(match.group(1)), int(match.group(2))) if match else (12, 31)
    try:
        return date(year, month, day)
    except ValueError:  # --02-29 in a common year
        return date(year, month, 28)


def parse_form_d(xml_text: str) -> dict[str, object] | None:
    """Read the offering size and security type from a Form D notice."""
    root = ET.fromstring(xml_text)

    def text(path: str) -> str | None:
        element = root.find(path)
        return element.text.strip() if element is not None and element.text else None

    sold = _number(text(".//offeringSalesAmounts/totalAmountSold"))
    offered = _number(text(".//offeringSalesAmounts/totalOfferingAmount"))
    types = root.find(".//offeringData/typesOfSecuritiesOffered")
    flags = {child.tag for child in types} if types is not None else set()
    if "isPooledInvestmentFundType" in flags:
        return None
    if "isEquityType" in flags:
        kind = "Private round"
    elif "isDebtType" in flags:
        kind = "Private debt"
    else:
        kind = "Private placement"
    first_sale = text(".//typeOfFiling/dateOfFirstSale/value")
    investors = text(".//investors/totalNumberAlreadyInvested")
    summary = f"Form D exempt offering: {_short_usd(sold)} sold of {_short_usd(offered)} offered"
    if first_sale:
        summary += f"; first sale {first_sale}"
    if investors and investors != "0":
        summary += f"; {investors} investors"
    return {
        "kind": kind,
        "amount": sold,
        "currency": "USD",
        "offering_amount": offered,
        "first_sale_date": first_sale,
        "summary": summary + ".",
    }


def parse_filing_event(
    form: str,
    items: str,
    document: str,
    *,
    filed: date,
) -> dict[str, object] | None:
    """Classify a financing filing and pull its headline amount from the text."""
    text = _document_text(document)
    item_set = {item.strip() for item in items.split(",") if item.strip()}
    if form == "8-K":
        body = _item_section(text, item_set)
    elif form == "6-K":
        body = _six_k_body(text)
    else:
        body = _PROSPECTUS_BOILERPLATE_RE.sub(" ", text[_prospectus_start(text) :])
        body = re.sub(r"\s+", " ", body)
    body = _ITEM_TITLE_RE.sub("", body.strip())
    if not body:
        return None
    lead = body[:700]
    if form in _PROSPECTUS_FORMS:
        kind = _classify_prospectus(lead)
    else:
        kind = _classify(form, item_set, lead)
    if kind is None:
        return None
    money = _first_amount(body[:1500])
    event: dict[str, object] = {
        "kind": kind,
        "amount": money[1] if money else None,
        "currency": money[0] if money else None,
        "summary": _truncate(body, 320),
    }
    if KIND_CATEGORY.get(kind) == "debt":
        event.update(parse_debt_terms(kind, body, filed=filed))
    return event


def parse_debt_terms(kind: str, body: str, *, filed: date) -> dict[str, object]:
    """Read maturity, security and collateral from a debt filing's terms.

    Notes are unsecured unless the filing says otherwise; loan facilities are
    left unknown when the text is silent, because many are secured by
    reference to a separate collateral agreement.
    """
    terms = body[:DEBT_TERMS_CHARS]
    if kind == "Convertible notes":
        secured: bool | None = bool(_SECURED_CONVERTIBLE_RE.search(terms))
    elif _SECURED_RE.search(terms):
        secured = True
    elif _UNSECURED_RE.search(terms) or kind == "Senior notes":
        secured = False
    else:
        secured = None
    collateral = (
        [label for label, pattern in _COLLATERAL_PATTERNS if pattern.search(terms)]
        if secured
        else []
    )
    amortization = _AMORTIZATION_RE.search(body)
    amortization_start: str | None = None
    frequency: int | None = None
    if amortization:
        month, day, year = amortization.group("month", "day", "year")
        try:
            amortization_start = date(
                int(year), _MONTHS.index(month.lower()) + 1, int(day)
            ).isoformat()
        except ValueError:
            amortization_start = None
        window = body[max(amortization.start() - 160, 0) : amortization.end()].lower()
        frequency = next(
            (count for word, count in _PAYMENT_FREQUENCIES if word in window), None
        )
    return {
        "maturity": _maturity(body, filed),
        "secured": secured,
        "collateral": collateral,
        # Amendments restate or upsize an existing facility: they stay in the
        # ledger but must not count as a new instrument.
        "amendment": bool(_AMENDMENT_RE.search(body[:300])),
        "rate": _rate(kind, terms),
        "amortization_start": amortization_start,
        "amortization_frequency": frequency if amortization_start else None,
    }


def _rate(kind: str, terms: str) -> str | None:
    """Coupon ("9.625%") or floating spread ("SOFR + 2.25%") as the filing states it.

    Loans quote a spread over SOFR; the base-rate alternative ("federal funds
    rate plus 0.50%") is never taken for the spread. Notes quote a coupon.
    """
    floating = next(
        (match for pattern in _FLOATING_RATE_PATTERNS if (match := pattern.search(terms))), None
    )
    fixed = _COUPON_RE.search(terms) or _PER_ANNUM_RE.search(terms)
    if floating and (kind == "Loan facility" or fixed is None):
        low, high = floating.group("low"), floating.groupdict().get("high")
        spread = f"{float(low):g}%" if not high else f"{float(low):g}–{float(high):g}%"
        return f"SOFR + {spread}"
    if fixed:
        return f"{float(fixed.group(1)):g}%"
    return None


def _maturity(body: str, filed: date) -> str | None:
    """Maturity as an ISO date, or a bare year when only "Notes due YYYY" is given."""
    for match in _MATURITY_DATE_RE.finditer(body):
        month, day, year = match.group("month", "day", "year")
        try:
            maturity = date(int(year), _MONTHS.index(month.lower()) + 1, int(day))
        except ValueError:
            continue
        if maturity > filed:
            return maturity.isoformat()
    anniversary = _ANNIVERSARY_RE.search(body)
    if anniversary:
        return _add_years(filed, _ORDINALS[anniversary.group(1).lower()]).isoformat()
    days = _DAYS_AFTER_RE.search(body)
    if days:
        return (filed + timedelta(days=int(days.group(1)))).isoformat()
    years = _YEARS_AFTER_RE.search(body)
    if years:
        count = years.group(1).lower()
        return _add_years(filed, int(count) if count.isdigit() else _ORDINALS[count]).isoformat()
    due = _DUE_YEAR_RE.search(body)
    return due.group(1) if due else None


def _add_years(start: date, years: int) -> date:
    try:
        return start.replace(year=start.year + years)
    except ValueError:  # 29 February
        return start.replace(year=start.year + years, day=28)


def maturity_date(maturity: str) -> date:
    """A bare maturity year stands for mid-year when measuring tenor."""
    return date(int(maturity), 7, 1) if len(maturity) == 4 else date.fromisoformat(maturity)


def _payment_dates(start: date, end: date, per_year: int) -> list[date]:
    step = 12 // per_year
    dates: list[date] = []
    current = start
    while current <= end:
        dates.append(current)
        month = current.month - 1 + step
        year = current.year + month // 12
        try:
            current = current.replace(year=year, month=month % 12 + 1)
        except ValueError:  # day past month end
            current = current.replace(year=year, month=month % 12 + 1, day=28)
    return dates


def _document_text(document: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", document)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\u200b", " ")
    text = re.sub(r"\s+", " ", text)
    # Inline XBRL splits long figures across tags: "$1,750, 000,000".
    return re.sub(r"(\d),\s+(\d{3})\b", r"\1,\2", text)


def _item_section(text: str, items: set[str]) -> str:
    headings = [(match.start(), match.group(1)) for match in _ITEM_RE.finditer(text)]
    for wanted in ("1.01", "2.03", "3.02"):
        if wanted not in items:
            continue
        for index, (position, item) in enumerate(headings):
            if item != wanted:
                continue
            end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
            if end - position > 200:
                return text[position:end]
    return ""


def _prospectus_start(text: str) -> int:
    for marker in ("PROSPECTUS SUPPLEMENT", "PROSPECTUS"):
        position = text.find(marker)
        if position >= 0:
            return position
    match = re.search(r"Rule 424", text, re.IGNORECASE)
    return match.start() if match else 0


def _six_k_body(text: str) -> str:
    # The cover's last "Form 20-F [x] Form 40-F [ ]" checkbox precedes the report.
    checkboxes = list(re.finditer(r"Form 20-F\s*\S{1,2}\s*Form 40-F\s*\S?", text))
    body = text[checkboxes[-1].end() :] if checkboxes else text
    body = re.sub(r"Indicate by check mark.*?\(\d\):\s*\S?", " ", body)
    body = re.sub(
        r"INCORPORATION BY REFERENCE.*?(?:furnished|filed)\.", " ", body, flags=re.IGNORECASE
    )
    if "INDEX TO EXHIBITS" in body:
        body = body.split("INDEX TO EXHIBITS", 1)[1]
        body = re.sub(r"^\s*Exhibit No\.\s*Description\s*(?:\d+\.\d+\s*)?", "", body)
    body = body.split("SIGNATURES", 1)[0]
    body = re.sub(r"^[\s¨☐☒x.]+", "", body)
    return re.sub(r"^(?:EXPLANATORY NOTE|INTRODUCTION)\s+", "", body)


def _classify(form: str, items: set[str], lead: str) -> str | None:
    if _MERGER_RE.search(lead):
        return None
    # Exhibit-only 6-Ks also carry status updates on legacy instruments.
    if form == "6-K" and re.search(r"\bupdate on\b", lead, re.IGNORECASE):
        return None
    if "3.02" in items and "1.01" not in items:
        return "Equity offering"
    if _CONVERTIBLE_RE.search(lead):
        return "Convertible notes"
    if _ATM_RE.search(lead):
        return "ATM program"
    if _NOTES_RE.search(lead):
        return "Senior notes"
    if _LOAN_RE.search(lead):
        return "Loan facility"
    if _EQUITY_RE.search(lead):
        return "Equity offering"
    if _LEASE_RE.search(lead):
        return "Lease"
    if "3.02" in items:
        return "Equity offering"
    if form == "6-K":
        return "Contract" if _CONTRACT_6K_RE.search(lead) else None
    return "Contract"


def _classify_prospectus(lead: str) -> str | None:
    # The cover title names the offered security; later cover text often
    # mentions concurrent deals or the facility the proceeds repay.
    title = lead[:250]
    if _CONVERTIBLE_RE.search(title):
        return "Convertible notes"
    if _NOTES_RE.search(title):
        return "Senior notes"
    if _ATM_RE.search(lead):
        return "ATM program"
    if _SHARES_RE.search(title):
        return "Equity offering"
    return None


def _first_amount(text: str) -> tuple[str, float] | None:
    for match in _MONEY_RE.finditer(text):
        symbol, digits, scale = match.groups()
        try:
            value = float(digits.rstrip(",.").replace(",", ""))
        except ValueError:
            continue
        value *= _SCALES.get((scale or "").lower(), 1.0)
        if value >= MIN_DEAL_AMOUNT:
            return _CURRENCIES.get(symbol.upper(), "USD"), value
    return None


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _short_usd(value: float | None) -> str:
    if value is None:
        return "undisclosed"
    if value >= 1e9:
        return f"${value / 1e9:,.2f}B"
    return f"${value / 1e6:,.1f}M"


# --- Payload ----------------------------------------------------------------


def compose_payload(
    companies: tuple[NeocloudCompany, ...],
    financials: dict[str, list[dict[str, object]]],
    events: list[dict[str, object]],
    schedules: dict[str, dict[str, object]] | None = None,
    *,
    today: date,
) -> dict[str, object]:
    names = {company.key: company.name for company in companies}
    merged = _merge_events(events)
    year_ago = (today - timedelta(days=365)).isoformat()

    public_rows: list[dict[str, object]] = []
    series: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "capex": 0.0,
            "net_financing": 0.0,
            "announced_debt": 0.0,
            "announced_equity": 0.0,
            "companies": set(),
        }
    )
    for company in companies:
        if not company.listed:
            continue
        by_metric: dict[str, dict[str, float]] = defaultdict(dict)
        for row in financials.get(company.key, []):
            by_metric[str(row["metric"])][str(row["period_end"])] = float(cast(float, row["value"]))
        balance_dates = {*by_metric.get("cash", {}), *by_metric.get("debt", {})}
        if not balance_dates:
            continue
        period_end = max(balance_dates)
        oldest_fallback = (
            date.fromisoformat(period_end) - timedelta(days=INSTANT_FALLBACK_DAYS)
        ).isoformat()
        values: dict[str, float | None] = {}
        stale: dict[str, str] = {}
        for metric in _INSTANT_METRICS:
            # Issuers drop a tag for a quarter when they restructure a line;
            # the last reported balance is kept and flagged with its date.
            history = by_metric.get(metric, {})
            dated = [end for end in history if oldest_fallback <= end <= period_end]
            if not dated:
                values[metric] = None
                continue
            latest = max(dated)
            values[metric] = history[latest]
            if latest != period_end:
                stale[metric] = latest
        for metric in ("revenue_ttm", "capex_ttm", "interest_ttm", "net_financing_ttm"):
            values[metric] = by_metric.get(metric, {}).get(period_end)

        for metric in ("capex", "net_financing"):
            for end, value in by_metric.get(f"{metric}_q", {}).items():
                bucket = series[_calendar_quarter(end)]
                bucket[metric] += value
                bucket["companies"].add(company.key)

        debt, cash = values["debt"], values["cash"]
        revenue, capex = values["revenue_ttm"], values["capex_ttm"]
        public_rows.append(
            {
                "key": company.key,
                "name": company.name,
                "segment": company.segment,
                "period_end": period_end,
                **values,
                "net_debt": debt - cash if debt is not None and cash is not None else None,
                "capex_to_revenue": round(capex / revenue, 2) if capex and revenue else None,
                "stale": stale,
            }
        )
    public_rows.sort(key=lambda row: _number(row["debt"]) or 0.0, reverse=True)

    for event in merged:
        amount = _number(event["amount"])
        if amount is None or event["currency"] != "USD":
            continue
        if event["category"] in {"debt", "equity"}:
            bucket = series[_calendar_quarter(str(event["date"]))]
            bucket[f"announced_{event['category']}"] += amount

    private_rows: list[dict[str, object]] = []
    for company in companies:
        if company.listed:
            continue
        own = [event for event in merged if event["company"] == company.key]
        rounds = [event for event in own if event["form"] in {"D", "D/A"}]
        ipo = next((event for event in own if event["kind"] == "IPO filing"), None)
        if not rounds and ipo is None:
            continue
        last = rounds[0] if rounds else None
        private_rows.append(
            {
                "key": company.key,
                "name": company.name,
                "segment": company.segment,
                "raised": sum(_number(event["amount"]) or 0.0 for event in rounds),
                "raised_12m": sum(
                    _number(event["amount"]) or 0.0
                    for event in rounds
                    if str(event["date"]) >= year_ago
                ),
                "rounds": len(rounds),
                "last_date": last["date"] if last else None,
                "last_amount": last["amount"] if last else None,
                "last_kind": last["kind"] if last else None,
                "ipo_filed": ipo["date"] if ipo else None,
            }
        )
    private_rows.sort(key=lambda row: _number(row["raised"]) or 0.0, reverse=True)

    recent = [event for event in merged if str(event["date"]) >= year_ago]

    def announced(category: str) -> float:
        return sum(
            _number(event["amount"]) or 0.0
            for event in recent
            if event["category"] == category and event["currency"] == "USD"
        )

    def total(field: str) -> float:
        return sum(_number(row[field]) or 0.0 for row in public_rows)

    current_quarter = _calendar_quarter(today.isoformat())
    quarter_rows = [
        {
            "period": period,
            "capex": round(bucket["capex"], 2),
            "net_financing": round(bucket["net_financing"], 2),
            "announced_debt": round(bucket["announced_debt"], 2),
            "announced_equity": round(bucket["announced_equity"], 2),
            "company_count": len(bucket["companies"]),
        }
        for period, bucket in sorted(series.items())
        if period <= current_quarter
    ][-SERIES_QUARTERS:]
    as_of = max((str(row["period_end"]) for row in public_rows), default=None)
    return {
        "as_of": as_of,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {"name": "SEC EDGAR", "url": SEC_SEARCH_URL},
        "methodology": {
            "label": "SEC filings ledger",
            "description": (
                "Listed issuers: balance-sheet and cash-flow lines from XBRL company facts; "
                "trailing figures sum the last four reported quarters, and a balance an issuer "
                "stopped tagging keeps its last reported date. Deals: 8-K items 1.01, 2.03 and "
                "3.02, 6-K reports and prospectus covers, with the headline amount read from "
                "the filing text (facility size or principal, not drawn balance) and launch, "
                "pricing and closing filings merged. Private issuers: Form D exempt-offering "
                "notices (amount sold) and IPO registrations; rounds that skip Form D and "
                "privately placed debt do not appear. Debt: rate, maturity, amortization start, "
                "security and collateral are read from each deal's terms (notes count as "
                "unsecured unless stated). The maturity wall uses each issuer's own 10-Q/10-K "
                "principal schedule where tagged, splits its open-ended last bucket by the "
                "issuer's deal maturities (estimate), adds deals signed after that report as "
                "commitments, and falls back to deal terms elsewhere. Amendments of existing "
                "facilities and non-USD tranches are excluded."
            ),
        },
        "summary": {
            "listed_companies": len(public_rows),
            "private_companies": len(private_rows),
            "total_debt": total("debt"),
            "total_cash": total("cash"),
            "total_leases": total("leases"),
            "total_backlog": total("backlog"),
            "ttm_capex": total("capex_ttm"),
            "ttm_net_financing": total("net_financing_ttm"),
            "private_raised_12m": sum(_number(row["raised_12m"]) or 0.0 for row in private_rows),
            "announced_debt_12m": announced("debt"),
            "announced_equity_12m": announced("equity"),
            "deals_12m": len(recent),
        },
        "series": quarter_rows,
        "companies": public_rows,
        "private_companies": private_rows,
        "debt": _debt_profile(
            merged,
            names,
            schedules or {},
            {str(row["key"]): _number(row["debt"]) for row in public_rows},
            today=today,
        ),
        "events": [
            {**event, "company_name": names.get(str(event["company"]), event["company"])}
            for event in merged[:EVENT_LIMIT]
        ],
    }


def _debt_profile(
    merged: list[dict[str, object]],
    names: dict[str, str],
    schedules: dict[str, dict[str, object]],
    balance_debt: dict[str, float | None],
    *,
    today: date,
) -> dict[str, object]:
    """Debt stack per issuer and the sector maturity wall.

    An issuer's own principal maturity schedule from its latest 10-Q/10-K is the
    base: it is outstanding debt and already reflects amortization. Its
    open-ended "after year N" bucket is split across years in proportion to the
    issuer's deal maturities (an estimate, kept as its own segment). Debt deals
    signed after that report are added as commitments. Issuers that tag no
    schedule fall back to deal terms: principal at maturity, or equal
    installments from a stated amortization start (also an estimate).
    """
    deals: dict[str, list[tuple[dict[str, object], float, str | None]]] = defaultdict(list)
    for event in merged:
        amount = _number(event["amount"])
        if event["category"] != "debt" or event["amendment"] or amount is None:
            continue
        if event["currency"] != "USD":
            continue
        maturity = str(event["maturity"]) if event["maturity"] else None
        if maturity is not None and maturity_date(maturity) <= today:
            continue
        deals[str(event["company"])].append((event, amount, maturity))

    due_year = today.year + 2
    sector: dict[int, dict[str, float]] = defaultdict(lambda: dict.fromkeys(WALL_SEGMENTS, 0.0))
    companies = []
    for key in names:
        rows = deals.get(key, [])
        schedule = schedules.get(key)
        buckets = cast(list[dict[str, Any]], schedule.get("buckets") or []) if schedule else []
        reported = bool(buckets) and schedule is not None and schedule.get("as_of") is not None
        if not rows and not reported:
            continue
        wall: dict[int, dict[str, float]] = defaultdict(lambda: dict.fromkeys(WALL_SEGMENTS, 0.0))
        unallocated: dict[str, object] | None = None
        undated = [(event, amount) for event, amount, maturity in rows if maturity is None]
        after_report: list[tuple[dict[str, object], float, str | None]] = []
        if reported and schedule is not None:
            as_of = str(schedule["as_of"])
            before = [row for row in rows if str(row[0]["date"]) <= as_of]
            after_report = [row for row in rows if str(row[0]["date"]) > as_of]
            for bucket in buckets:
                year, amount = int(bucket["year"]), float(bucket["amount"])
                if not bucket["open"]:
                    wall[year]["reported"] += amount
                    continue
                profile: dict[int, float] = defaultdict(float)
                # The bucket opens the day after the previous fiscal year ends.
                opens = _fiscal_year_end_date(year - 1, schedule.get("fiscal_year_end"))
                for event, face, maturity in before:
                    for paid_on, paid in _deal_payments(event, face, maturity, today):
                        if paid_on > opens:
                            profile[paid_on.year] += paid
                weight = sum(profile.values())
                if not weight:
                    unallocated = {"from_year": year, "amount": amount}
                    continue
                for paid_year, paid in profile.items():
                    wall[paid_year]["reported_split"] += amount * paid / weight
            for event, face, maturity in after_report:
                for paid_on, paid in _deal_payments(event, face, maturity, today):
                    wall[paid_on.year]["after_report"] += paid
        else:
            for event, face, maturity in rows:
                segment = "deals_amortizing" if event["amortization_start"] else "deals"
                for paid_on, paid in _deal_payments(event, face, maturity, today):
                    wall[paid_on.year][segment] += paid

        for year, segments in wall.items():
            for segment, amount in segments.items():
                sector[year][segment] += amount
        dated = [(event, amount, maturity) for event, amount, maturity in rows if maturity]
        tenor_weight = sum(amount for event, amount, _ in dated if event["tenor_years"] is not None)
        upcoming = sorted(dated, key=lambda row: maturity_date(str(row[2])))
        detail = _schedule_detail(schedule) if reported and schedule else None
        next_due = _next_due(detail, upcoming, after_report)
        companies.append(
            {
                "key": key,
                "name": names[key],
                "basis": "reported" if reported else "deals",
                "schedule": detail,
                "balance_sheet_debt": balance_debt.get(key),
                "instruments": len(rows),
                "face": sum(amount for _, amount, _ in rows),
                "secured_share": _secured_share(rows),
                "avg_tenor_years": (
                    round(
                        sum(
                            amount * float(cast(float, event["tenor_years"]))
                            for event, amount, _ in dated
                            if event["tenor_years"] is not None
                        )
                        / tenor_weight,
                        1,
                    )
                    if tenor_weight
                    else None
                ),
                "next_maturity": next_due[0] if next_due else None,
                "next_maturity_amount": next_due[1] if next_due else None,
                "next_maturity_source": next_due[2] if next_due else None,
                "after_report": {
                    "count": len(after_report),
                    "amount": sum(amount for _, amount, _ in after_report),
                },
                "undated": {"count": len(undated), "amount": sum(a for _, a in undated)},
                "unallocated": unallocated,
                "due_through": sum(
                    sum(segments.values()) for year, segments in wall.items() if year <= due_year
                ),
                "wall": _wall_rows(wall),
            }
        )
    companies.sort(
        key=lambda row: float(
            cast(float, cast(dict[str, Any], row["schedule"])["total"])
            if row["schedule"]
            else cast(float, row["face"])
        ),
        reverse=True,
    )
    all_deals = [row for rows in deals.values() for row in rows]
    return {
        "instruments": len(all_deals),
        "face": sum(amount for _, amount, _ in all_deals),
        "secured_share": _secured_share(all_deals),
        "reported_companies": sum(1 for row in companies if row["basis"] == "reported"),
        "due_through_year": due_year,
        "due_through": sum(float(cast(float, row["due_through"])) for row in companies),
        "undated": {
            "count": sum(cast(dict[str, int], row["undated"])["count"] for row in companies),
            "amount": sum(cast(dict[str, float], row["undated"])["amount"] for row in companies),
        },
        "unallocated": sum(
            float(cast(float, cast(dict[str, Any], row["unallocated"])["amount"]))
            for row in companies
            if row["unallocated"]
        ),
        "companies": companies,
        "maturity_wall": _wall_rows(sector),
    }


def _next_due(
    detail: dict[str, object] | None,
    upcoming: Sequence[tuple[dict[str, object], float, str | None]],
    after_report: Sequence[tuple[dict[str, object], float, str | None]],
) -> tuple[str, float, str] | None:
    """Nearest principal due as (label, amount, source).

    A reported schedule outranks deal maturities: deals signed before the report
    may already be repaid or amortizing, which only the schedule reflects. A deal
    signed after the report still wins when it falls due in an earlier year.
    """
    if detail is None:
        if not upcoming:
            return None
        _, amount, maturity = upcoming[0]
        return str(maturity), amount, "deal"
    buckets = cast(list[dict[str, Any]], detail["buckets"])
    first = next((bucket for bucket in buckets if float(bucket["amount"]) > 0), None)
    newer = sorted(
        (row for row in after_report if row[2]), key=lambda row: maturity_date(str(row[2]))
    )
    if newer and (first is None or maturity_date(str(newer[0][2])).year < int(first["year"])):
        return str(newer[0][2]), newer[0][1], "deal"
    if first is None:
        return None
    return str(first["label"]), float(first["amount"]), "reported"


def _deal_payments(
    event: dict[str, object],
    face: float,
    maturity: str | None,
    today: date,
) -> list[tuple[date, float]]:
    """Remaining principal payments of one deal.

    Principal is due at maturity unless the filing states an amortization
    start; then equal installments are assumed at the stated frequency
    (semi-annual when unstated) because the actual amounts sit in the indenture.
    """
    if maturity is None:
        return []
    end = maturity_date(maturity)
    start_text = event.get("amortization_start")
    start = date.fromisoformat(str(start_text)) if start_text else None
    if start is None or start >= end:
        return [(end, face)] if end > today else []
    per_year = int(cast(int, event.get("amortization_frequency") or 2))
    dates = _payment_dates(start, end, per_year)
    installment = face / len(dates)
    return [(paid, installment) for paid in dates if paid > today]


def _schedule_detail(schedule: dict[str, object]) -> dict[str, object]:
    buckets = cast(list[dict[str, Any]], schedule["buckets"])
    fiscal_year_end = str(schedule.get("fiscal_year_end") or "--12-31")
    prefix = "" if fiscal_year_end == "--12-31" else "FY"
    return {
        "as_of": schedule["as_of"],
        "form": schedule["form"],
        "url": schedule["url"],
        "fiscal_year_end": fiscal_year_end,
        "total": sum(float(bucket["amount"]) for bucket in buckets),
        "buckets": [
            {
                "label": f"{prefix}{bucket['year']}" + ("+" if bucket["open"] else "")
                + (" remainder" if bucket["offset"] == 0 else ""),
                "year": bucket["year"],
                "open": bucket["open"],
                "amount": bucket["amount"],
            }
            for bucket in buckets
        ],
    }


def _wall_rows(wall: dict[int, dict[str, float]]) -> list[dict[str, object]]:
    return [
        {"year": year, **{segment: round(value, 2) for segment, value in segments.items()}}
        for year, segments in sorted(wall.items())
        if any(segments.values())
    ]


def _secured_share(rows: list[tuple[dict[str, object], float, str | None]]) -> float | None:
    """Secured face over face whose security is stated; None when none is stated."""
    known = sum(amount for event, amount, _ in rows if event["secured"] is not None)
    secured = sum(amount for event, amount, _ in rows if event["secured"] is True)
    return round(secured / known, 3) if known else None


def _merge_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse launch/pricing/closing filings of one deal into a single row.

    Form D amendments restate the same offering, keyed by first-sale date.
    Debt and equity filings of one company and kind within EVENT_MERGE_DAYS are
    one deal; the latest filing wins and keeps the latest disclosed amount.
    Contracts and leases are always distinct agreements.
    """
    ordered = sorted(events, key=lambda event: (str(event["filed_date"]), str(event["accession"])))
    clusters: list[list[dict[str, object]]] = []
    open_clusters: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for event in ordered:
        filed = date.fromisoformat(str(event["filed_date"]))
        if event["form"] in {"D", "D/A"} and event.get("first_sale_date"):
            key = (str(event["company"]), "form-d", str(event["first_sale_date"]))
            cluster = open_clusters.get(key)
        elif KIND_CATEGORY.get(str(event["kind"])) in {"debt", "equity"}:
            key = (str(event["company"]), str(event["kind"]), "")
            cluster = open_clusters.get(key)
            if cluster is not None:
                last_filed = date.fromisoformat(str(cluster[-1]["filed_date"]))
                if (filed - last_filed).days > EVENT_MERGE_DAYS:
                    cluster = None
        else:
            key = (str(event["company"]), str(event["kind"]), str(event["accession"]))
            cluster = None
        if cluster is None:
            cluster = []
            clusters.append(cluster)
            open_clusters[key] = cluster
        cluster.append(event)

    merged: list[dict[str, object]] = []
    for cluster in clusters:
        latest = cluster[-1]

        def newest(field: str, rows: list[dict[str, object]] = cluster) -> dict[str, object]:
            return next(
                (event for event in reversed(rows) if event.get(field) is not None), rows[-1]
            )

        priced = newest("amount")
        termed = newest("secured")
        maturity = newest("maturity").get("maturity")
        issued = date.fromisoformat(str(cluster[0]["filed_date"]))
        kind = str(latest["kind"])
        merged.append(
            {
                "date": latest["filed_date"],
                "company": latest["company"],
                "form": latest["form"],
                "kind": kind,
                "category": KIND_CATEGORY.get(kind, "contract"),
                "amount": priced.get("amount"),
                "currency": priced.get("currency") if priced.get("amount") is not None else None,
                "offering_amount": latest.get("offering_amount"),
                "summary": latest["summary"],
                "url": latest["url"],
                "filings": len(cluster),
                "maturity": maturity,
                "tenor_years": (
                    round((maturity_date(str(maturity)) - issued).days / 365.25, 1)
                    if maturity
                    else None
                ),
                "secured": termed.get("secured"),
                "collateral": termed.get("collateral") or [],
                # The launch filing says whether the deal amends a facility.
                "amendment": bool(cluster[0].get("amendment")),
                "rate": newest("rate").get("rate"),
                "amortization_start": newest("amortization_start").get("amortization_start"),
                "amortization_frequency": newest("amortization_start").get(
                    "amortization_frequency"
                ),
            }
        )
    merged.sort(key=lambda event: str(event["date"]), reverse=True)
    return merged


def _calendar_quarter(period_end: str) -> str:
    parsed = date.fromisoformat(period_end)
    return f"{parsed.year}-Q{(parsed.month - 1) // 3 + 1}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
