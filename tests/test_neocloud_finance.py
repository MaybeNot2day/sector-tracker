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
    compose_payload,
    parse_company_facts,
    parse_debt_terms,
    parse_filing_event,
    parse_form_d,
    parse_reported_schedule,
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
        ("10-Q", "0000000001-26-000010", "2026-08-01", "", "acme-20260630.htm"),
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


def _instance(period_end: str, fiscal_year_end: str, facts: str) -> bytes:
    """A minimal 10-Q XBRL instance with USD unit and the given schedule facts."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:dei="http://xbrl.sec.gov/dei/2024"
    xmlns:us-gaap="http://fasb.org/us-gaap/2024" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
    xmlns:acme="http://acme.test/2026">
  <xbrli:context id="duration"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier>
    </xbrli:entity><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate>
    <xbrli:endDate>{period_end}</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="balance"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier>
    </xbrli:entity><xbrli:period><xbrli:instant>{period_end}</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="notes"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:LongtermDebtTypeAxis">
    us-gaap:SeniorNotesMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:instant>{period_end}</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="converts"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:LongtermDebtTypeAxis">
    us-gaap:ConvertibleDebtMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:instant>{period_end}</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <dei:DocumentPeriodEndDate contextRef="duration">{period_end}</dei:DocumentPeriodEndDate>
  <dei:CurrentFiscalYearEndDate contextRef="duration"
    >{fiscal_year_end}</dei:CurrentFiscalYearEndDate>
  {facts}
</xbrli:xbrl>""".encode()


def _maturity_fact(bucket: str, context: str, value: int) -> str:
    concept = f"LongTermDebtMaturitiesRepaymentsOfPrincipal{bucket}"
    return (
        f'<us-gaap:{concept} contextRef="{context}" unitRef="usd" decimals="-6">'
        f"{value}</us-gaap:{concept}>"
    )


ACME_INSTANCE = _instance(
    "2026-06-30",
    "--12-31",
    "".join(
        (
            _maturity_fact("RemainderOfFiscalYear", "balance", 100_000_000),
            _maturity_fact("InNextTwelveMonths", "balance", 200_000_000),
            '<us-gaap:LongTermDebtMaturityAfterYearFour contextRef="balance" unitRef="usd">'
            "1000000000</us-gaap:LongTermDebtMaturityAfterYearFour>",
            # Ignored: a per-type breakdown of the same schedule, and interest.
            _maturity_fact("RemainderOfFiscalYear", "notes", 100_000_000),
            '<us-gaap:LongTermDebtMaturitiesRepaymentsOfInterestInNextTwelveMonths'
            ' contextRef="balance" unitRef="usd">90000000'
            "</us-gaap:LongTermDebtMaturitiesRepaymentsOfInterestInNextTwelveMonths>",
        )
    ),
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
        if path == "/Archives/edgar/data/1/000000000126000010/index.json":
            names = ("acme-20260630.htm", "acme-20260630_htm.xml", "acme-20260630_lab.xml")
            return httpx.Response(200, json={"directory": {"item": [{"name": n} for n in names]}})
        if path == "/Archives/edgar/data/1/000000000126000010/acme-20260630_htm.xml":
            return httpx.Response(200, content=ACME_INSTANCE)
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

    # The issuer's own 10-Q schedule is the base. Its open "after year four"
    # bucket is dated by the only deal maturing then (the loan, May 2031).
    debt = cast(dict[str, Any], payload["debt"])
    wall = {row["year"]: row for row in cast(list[dict[str, Any]], debt["maturity_wall"])}
    assert sorted(wall) == [2026, 2027, 2031]
    assert (wall[2026]["reported"], wall[2027]["reported"]) == (100e6, 200e6)
    assert (wall[2031]["reported"], wall[2031]["reported_split"]) == (0.0, 1e9)
    stack = cast(list[dict[str, Any]], debt["companies"])
    acme_debt = stack[0]
    assert (acme_debt["key"], acme_debt["basis"], acme_debt["instruments"]) == (
        "ACME",
        "reported",
        1,
    )
    assert acme_debt["schedule"]["total"] == 1.3e9
    assert [bucket["label"] for bucket in acme_debt["schedule"]["buckets"]] == [
        "2026 remainder",
        "2027",
        "2031+",
    ]
    # The closing filing carries the terms; tenor runs from the launch filing.
    assert (acme_debt["secured_share"], acme_debt["avg_tenor_years"]) == (1.0, 5.0)
    # The reported schedule outranks deal dates: its first bucket is due sooner.
    assert (
        acme_debt["next_maturity"],
        acme_debt["next_maturity_amount"],
        acme_debt["next_maturity_source"],
    ) == ("2026 remainder", 100e6, "reported")
    assert loans[0]["collateral"] == ["GPUs", "Contracts"]
    assert loans[0]["rate"] is None

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
        pytest.param(
            "8-K",
            "1.01,2.03",
            "<p>Item 1.01 Entry into a Material Definitive Agreement.</p><p>Exit Credit"
            " Agreement On the Effective Date, the Company entered into a credit and guaranty"
            " agreement (the “Exit Credit Agreement”), consisting of an $80 million first-lien"
            " credit facility with certain holders of the Company’s April Convertible Notes and"
            " August Convertible Notes (the “Exit Lenders”) equal to (i) a $40 million term loan"
            f" and (ii) a $40 million roll-up of the outstanding balance.{FILLER}</p>"
            "<p>Item 9.01 Financial Statements and Exhibits.</p>",
            "Loan facility",
            8e7,
            id="exit-facility-lent-by-convertible-holders",
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
        pytest.param(
            "Loan facility",
            "Availability and Maturity Borrowings under the DDTL and issuances of Notes will be"
            " made in tranches, and are available until May 29, 2027, subject to certain"
            " extensions (the “Delayed Draw Availability Period”). The maturity date for"
            " borrowings under the DDTL and the Notes is December 31, 2031, or such earlier date"
            " as the final service fee has been paid in full.",
            "2031-12-31",
            None,
            False,
            id="maturity-not-draw-period-end",
        ),
        pytest.param(
            "Convertible notes",
            "Nebius Group N.V. entered into subscription agreements for the private placement of"
            " senior unsecured convertible notes in an aggregate principal amount of $1 billion.",
            None,
            False,
            False,
            id="senior-unsecured-convertible-is-unsecured",
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


def test_reported_schedule_sums_debt_types_on_a_non_calendar_fiscal_year() -> None:
    instance = _instance(
        "2026-08-31",
        "--05-31",
        "".join(
            (
                _maturity_fact("RemainderOfFiscalYear", "notes", 100_000_000),
                _maturity_fact("RemainderOfFiscalYear", "converts", 10_000_000),
                _maturity_fact("InYearTwo", "notes", 300_000_000),
                _maturity_fact("AfterYearFive", "converts", 450_000_000),
            )
        ),
    )

    schedule = parse_reported_schedule(instance)

    # August 2026 falls in the fiscal year ending May 2027.
    assert schedule["as_of"] == "2026-08-31"
    assert schedule["buckets"] == [
        {"offset": 0, "year": 2027, "open": False, "amount": 110_000_000},
        {"offset": 2, "year": 2029, "open": False, "amount": 300_000_000},
        {"offset": 6, "year": 2033, "open": True, "amount": 450_000_000},
    ]


def test_deal_terms_spread_stated_amortization_into_equal_installments() -> None:
    event = {
        "accession": "0000000001-26-000020",
        "company": "ACME",
        "form": "8-K",
        "filed_date": "2026-06-10",
        "kind": "Senior notes",
        "amount": 1e9,
        "currency": "USD",
        "offering_amount": None,
        "first_sale_date": None,
        "summary": "Senior secured notes.",
        "url": "https://www.sec.gov/Archives/edgar/data/1/x/notes.htm",
        "maturity": "2028-11-30",
        "secured": True,
        "collateral": ["Data centers"],
        "amendment": False,
        "rate": "6.129%",
        "amortization_start": "2027-05-30",
        "amortization_frequency": 2,
    }

    payload = compose_payload(COMPANIES, {}, [event], {}, today=TODAY)

    debt = cast(dict[str, Any], payload["debt"])
    assert [
        (row["year"], row["deals_amortizing"], row["deals"]) for row in debt["maturity_wall"]
    ] == [(2027, 5e8, 0.0), (2028, 5e8, 0.0)]
    assert debt["companies"][0]["basis"] == "deals"


@pytest.mark.parametrize(
    ("kind", "terms", "rate", "amortization"),
    [
        pytest.param(
            "Loan facility",
            "Amounts borrowed are subject to an interest rate per annum equal to (i) for SOFR"
            " loans, Term SOFR (subject to a 0.00% floor) plus an applicable margin of 5.50% per"
            " annum, and (ii) for base rate loans, the federal funds effective rate plus 0.50%"
            " and Term SOFR for a one-month tenor plus 1.00%.",
            "SOFR + 5.5%",
            (None, None),
            id="loan-spread-not-base-rate-definition",
        ),
        pytest.param(
            "Senior notes",
            "The Issuer completed its offering of 6.129% Senior Secured Notes due 2042. The"
            " principal amount of the Notes will amortize on a semi-annual basis on May 30 and"
            " November 30 of each year, beginning on May 30, 2030.",
            "6.129%",
            ("2030-05-30", 2),
            id="amortizing-project-notes",
        ),
        pytest.param(
            "Loan facility",
            "The Fifth Amendment adds an incremental $3.0 billion tranche of delayed draw term"
            " loans. Prior to the Fifth Amendment, amounts borrowed as term SOFR loans were"
            " subject to an interest rate per annum equal to (x) for investment-grade loans, an"
            " applicable margin of 6.00-6.50% plus the term SOFR (subject to a 0.00% floor) and"
            " (y) for non-investment-grade loans, an applicable margin of 13.00% plus the term"
            " SOFR for a three-month interest period (subject to a 0.00% floor). All amounts"
            " borrowed under the Fifth Amendment DDTL going forward are subject to an interest"
            " rate per annum equal to, at our option, either (a) for base rate loans, an"
            " applicable margin of 3.25% plus a base rate or (b) for term SOFR loans, 4.25% plus"
            " the term SOFR for a three-month interest period.",
            "SOFR + 4.25%",
            (None, None),
            id="amended-pricing-not-prior-pricing",
        ),
    ],
)
def test_debt_rate_and_amortization(
    kind: str, terms: str, rate: str, amortization: tuple[str | None, int | None]
) -> None:
    parsed = parse_debt_terms(kind, terms, filed=date(2026, 6, 10))

    assert parsed["rate"] == rate
    assert (parsed["amortization_start"], parsed["amortization_frequency"]) == amortization


def test_reported_schedule_counting_years_from_the_current_fiscal_year() -> None:
    # Hut 8's June 2026 10-Q: its table reads 2026 —, 2027 235,104, ..., 2030
    # 237,971, thereafter 7,075,006, tagged as years two to five and after five.
    hut = _instance(
        "2026-06-30",
        "--12-31",
        "".join(
            (
                _maturity_fact("InYearTwo", "balance", 235_104_000),
                _maturity_fact("InYearThree", "balance", 86_389_000),
                _maturity_fact("InYearFour", "balance", 100_634_000),
                _maturity_fact("InYearFive", "balance", 237_971_000),
                _maturity_fact("AfterYearFive", "balance", 7_075_006_000),
            )
        ),
    )
    # Core Scientific tags the empty early years as zeros: no shift.
    core = _instance(
        "2026-06-30",
        "--12-31",
        "".join(
            (
                _maturity_fact("RemainderOfFiscalYear", "balance", 0),
                _maturity_fact("InNextTwelveMonths", "balance", 0),
                _maturity_fact("InYearTwo", "balance", 0),
                _maturity_fact("InYearThree", "balance", 649_750_000),
            )
        ),
    )

    def years(instance: bytes) -> list[tuple[int, bool]]:
        buckets = cast(list[dict[str, Any]], parse_reported_schedule(instance)["buckets"])
        return [(bucket["year"], bucket["open"]) for bucket in buckets]

    assert years(hut) == [(2027, False), (2028, False), (2029, False), (2030, False), (2031, True)]
    assert years(core) == [(2029, False)]


@pytest.mark.asyncio
async def test_schedules_read_by_an_older_parser_are_read_again(tmp_path: Path) -> None:
    database = tmp_path / "board.sqlite3"
    db.save_neocloud_debt_schedule(
        database,
        {
            "company": "ACME",
            "accession": "0000000001-26-000010",
            "form": "10-Q",
            "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000010/acme.htm",
            "as_of": "2026-06-30",
            "fiscal_year_end": "--12-31",
            "buckets": [{"offset": 1, "year": 2027, "open": False, "amount": 5e6}],
        },
        parser_version=PARSER_VERSION - 1,
    )
    sec = FakeSec()
    service, client = _service(database, sec)

    payload = await service.get_financing()

    assert "/Archives/edgar/data/1/000000000126000010/acme-20260630_htm.xml" in sec.paths
    stack = cast(list[dict[str, Any]], cast(dict[str, Any], payload["debt"])["companies"])
    assert stack[0]["schedule"]["total"] == 1.3e9
    await client.aclose()


def _usd(*facts: tuple[str, float]) -> dict[str, Any]:
    facts_list = [_fact(None, end, value, "10-Q", "2026-08-05") for end, value in facts]
    return {"units": {"USD": facts_list}}


def _balance_debt(concepts: dict[str, float], end: str = "2026-03-31") -> float | None:
    payload = {"facts": {"us-gaap": {name: _usd((end, value)) for name, value in concepts.items()}}}
    rows = parse_company_facts(payload)
    return next((row["value"] for row in rows if row["metric"] == "debt"), None)


def test_balance_sheet_debt_adds_separately_presented_lines() -> None:
    # TeraWulf, March 2026: converts and short-term borrowings sit beside LongTermDebt.
    terawulf = _balance_debt(
        {
            "LongTermDebt": 3_103.8e6,
            "ConvertibleDebtNoncurrent": 1_597.3e6,
            "ConvertibleDebtCurrent": 490.4e6,
            "ShortTermBorrowings": 98.6e6,
            "Liabilities": 7_086.4e6,
        }
    )
    # WhiteFiber, June 2026: "Total term debt" (short-term loans included) plus converts.
    whitefiber = _balance_debt(
        {
            "DebtInstrumentCarryingAmount": 83.2e6,
            "ConvertibleLongTermNotesPayable": 222.6e6,
            "ConvertibleNotesPayable": 222.6e6,
            "ShortTermBorrowings": 29.3e6,
            "Liabilities": 543.5e6,
        },
        end="2026-06-30",
    )
    # IREN, June 2025: its converts are its LongTermDebt; nothing to add.
    iren = _balance_debt(
        {
            "LongTermDebt": 962.8e6,
            "ConvertibleLongTermNotesPayable": 962.8e6,
            "ConvertibleNotesPayable": 962.8e6,
            "Liabilities": 1_122.8e6,
        },
        end="2025-06-30",
    )

    assert terawulf == pytest.approx(5_290.1e6)
    assert whitefiber == pytest.approx(305.8e6)
    assert iren == pytest.approx(962.8e6)


def test_negative_quarter_on_an_unsigned_line_is_not_interest() -> None:
    # WhiteFiber tagged Q1 2026 InterestExpense as -1,995,033.
    payload = {
        "facts": {
            "us-gaap": {
                "InterestExpense": {
                    "units": {
                        "USD": [
                            _fact("2026-01-01", "2026-03-31", -1_995_033, "10-Q", "2026-05-14"),
                            _fact("2026-04-01", "2026-06-30", 4_200_000, "10-Q", "2026-08-12"),
                        ]
                    }
                }
            }
        }
    }

    rows = parse_company_facts(payload)

    assert [(row["period_end"], row["value"]) for row in rows if row["metric"] == "interest_q"] == [
        ("2026-06-30", 4_200_000)
    ]


def _stored_events(
    database: Path, filings: list[tuple[str, str, str, str]]
) -> list[dict[str, Any]]:
    """Parse (accession, filed, form, document) filings and round-trip them through the store."""
    rows: list[dict[str, object]] = []
    for accession, filed, form, document in filings:
        row: dict[str, object] = {
            "accession": accession,
            "company": "ACME",
            "form": form,
            "filed_date": filed,
            "url": f"https://www.sec.gov/Archives/edgar/data/1/{accession}/doc.htm",
        }
        if form == "D":
            parsed = parse_form_d(document)
        else:
            items = "1.01,2.03" if form == "8-K" else ""
            parsed = parse_filing_event(form, items, document, filed=date.fromisoformat(filed))
        assert parsed is not None
        rows.append({**row, **parsed})
    db.save_neocloud_filings(database, rows, parser_version=PARSER_VERSION)
    return cast(list[dict[str, Any]], db.load_neocloud_events(database))


def _six_k(exhibits: str) -> str:
    return (
        "<p>Form 20-F x Form 40-F ¨</p><p>INDEX TO EXHIBITS Exhibit No. Description"
        f" {exhibits}</p><p>SIGNATURES</p>"
    )


def test_multi_tranche_offering_sums_tranches_and_dates_each(tmp_path: Path) -> None:
    iren = _eight_k(
        "On December 8, 2025 (the “Closing Date”), IREN Limited issued $1.15 billion aggregate"
        " principal amount of its 0.25% convertible senior notes due 2032 (the “2032 Notes”)"
        " and $1.15 billion aggregate principal amount of its 1.00% convertible senior notes"
        " due 2033 (the “2033 Notes”). The 2032 Notes will mature on June 1, 2032 and the 2033"
        " Notes will mature on June 1, 2033."
    )
    # Nebius, September 2025: the pricing 6-K gives the size, the closing 6-K
    # lists one indenture per series without amounts.
    pricing = _six_k(
        "99.2 Press release dated September 10, 2025, announcing the pricing of the Company’s"
        " upsized private offering of $2.75 billion of convertible senior notes"
    )
    closing = _six_k(
        "4.1 Indenture dated as of September 15, 2025, between the Company and U.S. Bank Trust"
        " Company, National Association, as trustee, for the 1.00% Convertible Senior Notes due"
        " 2030 4.2 Form of 1.00% Convertible Senior Note due 2030 (included in Exhibit 4.1) 4.3"
        " Indenture dated as of September 15, 2025, between the Company and U.S. Bank Trust"
        " Company, National Association, as trustee, for the 2.75% Convertible Senior Notes due"
        " 2032 99.1 Press release announcing the closing of the private offering of convertible"
        " senior notes"
    )
    events = _stored_events(
        tmp_path / "board.sqlite3",
        [
            ("0001140361-25-044888", "2025-12-08", "8-K", iren),
            ("0001410578-25-002149", "2025-09-12", "6-K", pricing),
            ("0001104659-25-089969", "2025-09-15", "6-K", closing),
        ],
    )

    payload = compose_payload(COMPANIES, {}, events, {}, today=TODAY)

    ledger = {
        event["date"]: (event["amount"], event["maturity"])
        for event in cast(list[dict[str, Any]], payload["events"])
    }
    assert ledger == {"2025-12-08": (2.3e9, "2032-06-01"), "2025-09-15": (2.75e9, "2030")}
    wall = {
        row["year"]: row["deals"]
        for row in cast(dict[str, Any], payload["debt"])["maturity_wall"]
    }
    # The exhibit list leaves the split unstated: equal halves, an estimate.
    assert wall == {2030: 1.375e9, 2032: 1.375e9 + 1.15e9, 2033: 1.15e9}


def test_reopened_notes_stay_a_separate_issue(tmp_path: Path) -> None:
    # CoreWeave's 9.750% notes due 2031: the original 8-K misprints its size.
    original = _eight_k(
        "On April 14, 2026, CoreWeave, Inc. completed its previously announced private offering"
        " of $1,750,000 aggregate principal amount of its 9.750% Senior Notes due 2031 (the"
        " “Senior Notes”). The Senior Notes will mature on October 1, 2031."
    )
    reopening = _eight_k(
        "On April 21, 2026, CoreWeave, Inc. completed its previously announced private offering"
        " of $1,000,000,000 aggregate principal amount of its 9.750% Senior Notes due 2031 (the"
        " “Additional Notes”). As previously announced, on April 14, 2026, CoreWeave issued"
        " $1,750,000,000 aggregate principal amount of 9.750% Senior Notes due 2031 under the"
        " Base Indenture (the “Existing Notes”), for a total of $2,750,000,000 aggregate"
        " principal amount of Notes outstanding. The Notes will mature on October 1, 2031."
    )
    events = _stored_events(
        tmp_path / "board.sqlite3",
        [
            ("0001769628-26-000164", "2026-04-14", "8-K", original),
            ("0001769628-26-000183", "2026-04-21", "8-K", reopening),
        ],
    )

    payload = compose_payload(COMPANIES, {}, events, {}, today=TODAY)

    notes = [
        (event["date"], event["amount"])
        for event in cast(list[dict[str, Any]], payload["events"])
    ]
    assert notes == [("2026-04-21", 1e9), ("2026-04-14", 1.75e9)]
    assert cast(dict[str, Any], payload["summary"])["announced_debt_12m"] == 2.75e9


def test_filings_restating_a_deal_are_counted_with_it(tmp_path: Path) -> None:
    # Nebius, June 2025: an exhibit-list 6-K names the indentures, the closing
    # 6-K carries the $1 billion. Applied Digital, September 2024: the 8-K and
    # the Form D of one $160 million private placement.
    exhibits = _six_k(
        "99.1 Press release dated June 2, 2025, announcing signing in respect of Private"
        " Placement 99.4 Form of Indenture for the 2029 Notes"
    )
    closing = _six_k(
        "As previously announced, Nebius entered into subscription agreements for the private"
        " placement of senior unsecured convertible notes in an aggregate principal amount of"
        " $1 billion. The Private Placement transaction closed on June 5, 2025."
    )
    placement = _eight_k(
        "On January 5, 2026, the Company entered into a securities purchase agreement for the"
        " private placement of 49,382,720 shares of common stock, par value $0.001 per share."
        " The Private Placement closed with aggregate gross proceeds of approximately $160"
        " million."
    )
    events = _stored_events(
        tmp_path / "board.sqlite3",
        [
            ("0001104659-25-055197", "2025-06-02", "6-K", exhibits),
            ("0001104659-25-056817", "2025-06-05", "6-K", closing),
            ("0001144879-26-000235", "2026-01-12", "8-K", placement),
            ("0001493152-26-037662", "2026-01-20", "D", _form_d(160_000_013)),
        ],
    )

    payload = compose_payload(COMPANIES, {}, events, {}, today=TODAY)

    ledger = [
        (event["kind"], event["amount"], event["filings"])
        for event in cast(list[dict[str, Any]], payload["events"])
    ]
    assert ledger == [("Equity offering", 160e6, 2), ("Convertible notes", 1e9, 2)]


def _debt_event(filed: str, amount: float, maturity: str, **fields: object) -> dict[str, Any]:
    return {
        "accession": f"0000000001-{filed}",
        "company": "ACME",
        "form": "8-K",
        "filed_date": filed,
        "kind": "Convertible notes",
        "amount": amount,
        "currency": "USD",
        "offering_amount": None,
        "first_sale_date": None,
        "summary": "Convertible senior notes.",
        "url": f"https://www.sec.gov/Archives/edgar/data/1/{filed}/notes.htm",
        "maturity": maturity,
        "secured": False,
        "collateral": [],
        "amendment": False,
        "rate": "0%",
        "amortization_start": None,
        "amortization_frequency": None,
        "tranches": [],
        **fields,
    }


def test_maturity_wall_uses_the_issuers_fiscal_years() -> None:
    # IREN (June year end): converts due July 1, 2031 fall in fiscal 2032.
    schedule = {
        "company": "ACME",
        "accession": "0000000001-26-000052",
        "form": "10-K",
        "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000052/acme.htm",
        "as_of": "2026-06-30",
        "fiscal_year_end": "--06-30",
        "buckets": [{"offset": 1, "year": 2027, "open": False, "amount": 177e6}],
    }
    event = _debt_event("2026-09-01", 1e9, "2031-07-01")

    payload = compose_payload(COMPANIES, {}, [event], {"ACME": schedule}, today=TODAY)

    wall = cast(dict[str, Any], payload["debt"])["maturity_wall"]
    assert [(row["year"], row["reported"], row["after_report"]) for row in wall] == [
        (2027, 177e6, 0.0),
        (2032, 0.0, 1e9),
    ]


def test_amendments_are_not_announced_as_new_debt() -> None:
    events = [
        _debt_event("2026-03-02", 2e9, "2031-03-01"),
        # CoreWeave's Fifth Amendment adds $3.0 billion to an existing facility.
        _debt_event("2025-10-02", 3e9, "2030-10-02", kind="Loan facility", amendment=True),
        _debt_event("2026-05-06", 5e8, "2030-05-06", kind="Loan facility", amendment=True),
    ]

    payload = compose_payload(COMPANIES, {}, events, {}, today=TODAY)

    assert cast(dict[str, Any], payload["summary"])["announced_debt_12m"] == 2e9
    series = {row["period"]: row["announced_debt"] for row in payload["series"]}
    assert series == {"2026-Q1": 2e9}


def test_balance_sheet_lagging_the_sector_is_flagged() -> None:
    companies = (
        NeocloudCompany("CRWV", "CoreWeave", "GPU cloud", 1, True),
        NeocloudCompany("NBIS", "Nebius", "GPU cloud", 2, True),
    )
    financials = {
        "CRWV": [
            {"period_end": "2026-06-30", "metric": "cash", "value": 3e9},
            {"period_end": "2026-06-30", "metric": "debt", "value": 35e9},
        ],
        # Nebius files a yearly 20-F: its latest balance sheet is December 2025.
        "NBIS": [
            {"period_end": "2025-12-31", "metric": "cash", "value": 3.7e9},
            {"period_end": "2025-12-31", "metric": "debt", "value": 4.1e9},
        ],
    }

    payload = compose_payload(companies, financials, [], {}, today=TODAY)

    stale = {row["key"]: row["stale"] for row in cast(list[dict[str, Any]], payload["companies"])}
    assert stale == {"CRWV": {}, "NBIS": {"cash": "2025-12-31", "debt": "2025-12-31"}}
