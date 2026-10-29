from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app import db
from app.services.neocloud_finance import (
    PARSER_VERSION,
    NeocloudCompany,
    NeocloudFinanceService,
    parse_debt_terms,
    parse_filing_event,
)

TODAY = date(2026, 10, 29)
COMPANIES = (
    NeocloudCompany("ACME", "Acme Cloud", "GPU cloud", 1, True),
    NeocloudCompany("PRIV", "Priv Compute", "GPU cloud", 2, False),
)
FILLER = (
    " The facility is secured by the borrower's GPU fleet and customer contracts, and"
    " borrowings bear interest at a floating rate plus an applicable margin."
)


def _fact(start: str | None, end: str, val: float, form: str, filed: str) -> dict[str, Any]:
    fact: dict[str, Any] = {"end": end, "val": val, "form": form, "filed": filed}
    if start is not None:
        fact["start"] = start
    return fact


COMPANY_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        _fact("2025-07-01", "2025-09-30", 100, "10-Q", "2025-11-01"),
                        _fact("2025-01-01", "2025-12-31", 400, "10-K", "2026-02-20"),
                        _fact("2026-01-01", "2026-03-31", 100, "10-Q", "2026-05-01"),
                        _fact("2026-01-01", "2026-06-30", 250, "10-Q", "2026-08-01"),
                    ]
                }
            },
            # The nine-month figure is tagged under a different revenue concept
            # than the fiscal year, so Q4 only exists after merging concepts.
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [_fact("2025-01-01", "2025-09-30", 280, "10-Q", "2025-11-01")]}
            },
            "LongTermDebt": {
                "units": {"USD": [_fact(None, "2026-03-31", 900, "10-Q", "2026-05-01")]}
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        _fact(None, "2026-03-31", 70, "10-Q", "2026-05-01"),
                        _fact(None, "2026-06-30", 50, "10-Q", "2026-08-01"),
                    ]
                }
            },
        }
    }
}


def _submissions(rows: list[tuple[str, str, str, str, str]]) -> dict[str, Any]:
    return {
        "filings": {
            "recent": {
                "form": [row[0] for row in rows],
                "accessionNumber": [row[1] for row in rows],
                "filingDate": [row[2] for row in rows],
                "items": [row[3] for row in rows],
                "primaryDocument": [row[4] for row in rows],
            },
            "files": [],
        }
    }


LISTED_FILINGS = _submissions(
    [
        ("8-K", "0000000001-26-000005", "2026-05-08", "1.01,2.03", "loan-close.htm"),
        ("8-K", "0000000001-26-000004", "2026-05-06", "1.01", "contract-b.htm"),
        ("8-K", "0000000001-26-000003", "2026-05-05", "1.01", "contract-a.htm"),
        ("8-K", "0000000001-26-000002", "2026-05-01", "1.01,2.03", "loan-launch.htm"),
        ("8-K", "0000000001-26-000001", "2026-04-01", "5.02", "officer.htm"),
        ("4", "0000000001-26-000000", "2026-03-01", "", "form4.xml"),
    ]
)
PRIVATE_FILINGS = _submissions(
    [
        ("D/A", "0000000002-26-000002", "2026-05-12", "", "xslFormDX01/primary_doc.xml"),
        ("D", "0000000002-26-000001", "2026-01-23", "", "xslFormDX01/primary_doc.xml"),
    ]
)


def _eight_k(body: str) -> str:
    return (
        "<html><body><p>Item 1.01 Entry into a Material Definitive Agreement.</p>"
        f"<p>{body}{FILLER}</p><p>Item 9.01 Financial Statements and Exhibits.</p></body></html>"
    )


DOCUMENTS = {
    "loan-launch.htm": _eight_k(
        "On May 1, 2026, Acme Financing LLC entered into a credit agreement providing for a"
        " $2.5 billion delayed draw term loan facility."
    ),
    "loan-close.htm": _eight_k(
        "On May 7, 2026, Acme Financing LLC upsized its delayed draw term loan facility to"
        " $2.6 billion. The maturity date of the DDTL 2.0 Facility is May 7, 2031."
    ),
    "contract-a.htm": _eight_k(
        "On May 4, 2026, Acme Cloud entered into a master services agreement with a hyperscale"
        " customer with a total contract value of $14.2 billion."
    ),
    "contract-b.htm": _eight_k(
        "On May 5, 2026, Acme Cloud entered into a master services agreement with a model lab"
        " with a total contract value of $1.1 billion."
    ),
}


def _form_d(sold: int) -> str:
    return f"""<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer><entityName>Priv Compute Inc.</entityName></primaryIssuer>
  <offeringData>
    <typeOfFiling><dateOfFirstSale><value>2026-01-10</value></dateOfFirstSale></typeOfFiling>
    <typesOfSecuritiesOffered><isEquityType>true</isEquityType></typesOfSecuritiesOffered>
    <offeringSalesAmounts>
      <totalOfferingAmount>850000000</totalOfferingAmount>
      <totalAmountSold>{sold}</totalAmountSold>
    </offeringSalesAmounts>
    <investors><totalNumberAlreadyInvested>12</totalNumberAlreadyInvested></investors>
  </offeringData>
</edgarSubmission>"""


class FakeSec:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if self.down:
            return httpx.Response(503)
        if path == "/api/xbrl/companyfacts/CIK0000000001.json":
            return httpx.Response(200, json=COMPANY_FACTS)
        if path == "/submissions/CIK0000000001.json":
            return httpx.Response(200, json=LISTED_FILINGS)
        if path == "/submissions/CIK0000000002.json":
            return httpx.Response(200, json=PRIVATE_FILINGS)
        if path == "/Archives/edgar/data/2/000000000226000001/primary_doc.xml":
            return httpx.Response(200, text=_form_d(450_000_000))
        if path == "/Archives/edgar/data/2/000000000226000002/primary_doc.xml":
            return httpx.Response(200, text=_form_d(842_000_000))
        document = path.rsplit("/", 1)[-1]
        if path.startswith("/Archives/edgar/data/1/") and document in DOCUMENTS:
            return httpx.Response(200, text=DOCUMENTS[document])
        return httpx.Response(404)

    def archive_requests(self) -> list[str]:
        return [path for path in self.paths if path.startswith("/Archives/")]


def _service(database: Path, sec: FakeSec) -> tuple[NeocloudFinanceService, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(sec))
    service = NeocloudFinanceService(
        database,
        cache_seconds=0,
        request_interval=0,
        client=client,
        companies=COMPANIES,
        today=TODAY,
    )
    return service, client


@pytest.mark.asyncio
async def test_service_builds_ledger_and_balance_sheet_from_sec_filings(tmp_path: Path) -> None:
    sec = FakeSec()
    service, client = _service(tmp_path / "board.sqlite3", sec)

    payload = await service.get_financing()

    listed = cast(list[dict[str, Any]], payload["companies"])
    assert len(listed) == 1
    acme = listed[0]
    assert acme["period_end"] == "2026-06-30"
    assert acme["cash"] == 50
    # Q3 100 + Q4 (400 - 280) + Q1 100 + Q2 (250 - 100).
    assert acme["revenue_ttm"] == 470
    # Debt was not tagged on the latest balance sheet: last value, dated.
    assert acme["debt"] == 900
    assert acme["stale"] == {"debt": "2026-03-31"}

    events = cast(list[dict[str, Any]], payload["events"])
    loans = [event for event in events if event["kind"] == "Loan facility"]
    assert [(event["amount"], event["filings"], event["date"]) for event in loans] == [
        (2.6e9, 2, "2026-05-08")
    ]
    contracts = sorted(event["amount"] for event in events if event["kind"] == "Contract")
    assert contracts == [1.1e9, 14.2e9]
    rounds = [event for event in events if event["company"] == "PRIV"]
    assert [(event["kind"], event["amount"], event["filings"]) for event in rounds] == [
        ("Private round", 842e6, 2)
    ]

    private = cast(list[dict[str, Any]], payload["private_companies"])
    assert [(row["key"], row["raised"], row["last_date"]) for row in private] == [
        ("PRIV", 842e6, "2026-05-12")
    ]
    series = {row["period"]: row for row in cast(list[dict[str, Any]], payload["series"])}
    assert series["2026-Q2"]["announced_debt"] == 2.6e9
    assert series["2026-Q2"]["announced_equity"] == 842e6
    assert not any(path.endswith("officer.htm") for path in sec.paths)

    # The closing filing carries the terms; tenor runs from the launch filing.
    debt = cast(dict[str, Any], payload["debt"])
    assert debt["maturity_wall"] == [
        {"year": 2031, "secured": 2.6e9, "unsecured": 0.0, "unknown": 0.0, "count": 1}
    ]
    stack = cast(list[dict[str, Any]], debt["companies"])
    assert [
        (row["key"], row["instruments"], row["secured_share"], row["avg_tenor_years"])
        for row in stack
    ] == [("ACME", 1, 1.0, 5.0)]
    assert stack[0]["next_maturity"] == "2031-05-07"
    assert loans[0]["collateral"] == ["GPUs", "Contracts"]

    # Reviewed filings are never fetched again.
    fetched = len(sec.archive_requests())
    await service.get_financing()
    assert len(sec.archive_requests()) == fetched
    await client.aclose()


@pytest.mark.asyncio
async def test_service_serves_persisted_ledger_when_sec_is_down(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    sec = FakeSec()
    service, client = _service(database, sec)
    first = await service.get_financing()
    await client.aclose()

    sec.down = True
    restarted, client = _service(database, sec)
    payload = await restarted.get_financing()

    assert payload["events"] == first["events"]
    assert payload["companies"] == first["companies"]
    await client.aclose()


@pytest.mark.asyncio
async def test_filings_stored_by_an_older_parser_are_reviewed_again(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    url = "https://www.sec.gov/Archives/edgar/data/1/000000000126000005/loan-close.htm"
    db.save_neocloud_filings(
        database,
        [
            {
                "accession": "0000000001-26-000005",
                "company": "ACME",
                "form": "8-K",
                "filed_date": "2026-05-08",
                "kind": "Loan facility",
                "amount": 2.6e9,
                "currency": "USD",
                "url": url,
            }
        ],
        parser_version=PARSER_VERSION - 1,
    )
    sec = FakeSec()
    service, client = _service(database, sec)

    payload = await service.get_financing()

    assert "/Archives/edgar/data/1/000000000126000005/loan-close.htm" in sec.paths
    events = cast(list[dict[str, Any]], payload["events"])
    loans = [event for event in events if event["kind"] == "Loan facility"]
    assert loans[0]["maturity"] == "2031-05-07"
    await client.aclose()


@pytest.mark.parametrize(
    ("form", "items", "document", "kind", "amount"),
    [
        pytest.param(
            "424B5",
            "",
            "<p>PROSPECTUS SUPPLEMENT (To Prospectus dated April 14, 2026)</p>"
            "<p>$800,000,000 Common Stock</p><p>Acme Inc. is offering $800,000,000 of shares"
            " of its common stock, par value $0.001 per share, including repayment in full of"
            " amounts outstanding under our Bridge Credit Facility.</p>",
            "Equity offering",
            8e8,
            id="prospectus-cover-names-security-not-repaid-facility",
        ),
        pytest.param(
            "8-K",
            "3.02,9.01",
            "<p>Item 3.02 Unregistered Sales of Equity Securities.</p><p>On April 15, 2026,"
            " Acme Inc. agreed to issue 10,000,000 shares of Class A common stock, par value"
            f" $0.000005 per share, for aggregate consideration of $1.0 billion.{FILLER}</p>"
            "<p>Item 9.01 Financial Statements and Exhibits.</p>",
            "Equity offering",
            1e9,
            id="unregistered-equity-skips-par-value",
        ),
        pytest.param(
            "6-K",
            "",
            "<p>Form 20-F x Form 40-F ¨</p><p>INDEX TO EXHIBITS Exhibit No. Description 99.1"
            " Press release dated August 19, 2026, announcing the pricing of the Company's"
            " upsized private offering of €5.0 billion of convertible senior notes</p>"
            "<p>SIGNATURES</p>",
            "Convertible notes",
            5e9,
            id="foreign-issuer-exhibit-index",
        ),
    ],
)
def test_filing_event_classification(
    form: str, items: str, document: str, kind: str, amount: float
) -> None:
    event = parse_filing_event(form, items, document, filed=date(2026, 8, 20))

    assert event is not None
    assert (event["kind"], event["amount"]) == (kind, amount)


def test_merger_agreement_is_not_a_financing_event() -> None:
    document = _eight_k(
        "On May 5, 2026, Acme Inc. entered into an Agreement and Plan of Merger to acquire"
        " Target Corp. for $9.0 billion in stock."
    )

    assert parse_filing_event("8-K", "1.01", document, filed=date(2026, 5, 5)) is None


@pytest.mark.parametrize(
    ("kind", "terms", "maturity", "secured", "amendment"),
    [
        pytest.param(
            "Loan facility",
            "The DDTL 5.5 Facility provides for delayed draw term loans until December 2026."
            " The maturity date of the DDTL 5.5 Facility is September 1, 2031.",
            "2031-09-01",
            None,
            False,
            id="explicit-date-after-dotted-facility-name",
        ),
        pytest.param(
            "Loan facility",
            "Loans may be borrowed until the fourth anniversary of the Closing Date. The"
            " obligations are secured by a first-priority lien on the data center campus.",
            "2030-09-24",
            True,
            False,
            id="revolver-anniversary",
        ),
        pytest.param(
            "Loan facility",
            "The Term Loan Facility will mature on the date that is 364 days after the Closing"
            " Date. Loans bear interest at a rate per annum of 1.00% plus the six (6)-month"
            " anniversary step-up.",
            "2027-09-23",
            None,
            False,
            id="bridge-days-after-closing",
        ),
        pytest.param(
            "Convertible notes",
            "The Company issued 0.00% Convertible Senior Notes due 2032. The notes are"
            " effectively subordinated to senior secured notes of the Company's subsidiaries.",
            "2032",
            False,
            False,
            id="convertible-unsecured-despite-subsidiary-secured-debt",
        ),
        pytest.param(
            "Loan facility",
            "Amendment No. 1 to the Credit Agreement increases commitments by $250 million.",
            None,
            None,
            True,
            id="amendment-of-existing-facility",
        ),
    ],
)
def test_debt_terms(
    kind: str, terms: str, maturity: str | None, secured: bool | None, amendment: bool
) -> None:
    parsed = parse_debt_terms(kind, terms, filed=date(2026, 9, 24))

    assert (parsed["maturity"], parsed["secured"], parsed["amendment"]) == (
        maturity,
        secured,
        amendment,
    )
