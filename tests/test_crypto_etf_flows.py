from app.services.crypto_etf_flows import (
    parse_pipe_table,
    parse_token_table,
    summarize_flow_asset,
)


def test_parse_token_table_normalizes_bitcoin_farside_rows() -> None:
    markdown = """
Bitcoin ETF Flow - All Data (US$m)
Date
IBIT
FBTC
GBTC
Total

11 Jan 2024
111.7
227.0
(95.1)
243.6

12 Jan 2024
386.0
195.3
(484.1)
97.2
"""

    rows = parse_token_table(markdown)
    payload = summarize_flow_asset("BTC", "BTC Spot ETFs", rows)

    assert rows[0]["date"] == "2024-01-11"
    assert rows[0]["flow_usd"] == 243_600_000
    assert rows[0]["etf_flows"][2]["flow_usd"] == -95_100_000  # type: ignore[index]
    assert payload["latest_date"] == "2024-01-12"
    assert payload["latest_flow_usd"] == 97_200_000
    assert payload["five_day_flow_usd"] == 340_800_000


def test_parse_pipe_table_normalizes_ethereum_farside_rows() -> None:
    markdown = """
|  | Blackrock | Fidelity | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | ETHA | FETH | ETHE |  |
| Fee | 0.25% | 0.25% | 2.50% |  |
| Seed | 10.6 | 4.4 | 9,199.3* | 10,360 |
| 23 Jul 2024 | 266.5 | 71.3 | (484.1) | (146.3) |
| 24 Jul 2024 | 17.4 | - | (326.9) | (309.5) |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("ETH", "ETH Spot ETFs", rows)

    assert rows[0]["date"] == "2024-07-23"
    assert rows[0]["flow_usd"] == -146_300_000
    assert rows[0]["etf_flows"][0]["ticker"] == "ETHA"  # type: ignore[index]
    assert rows[0]["etf_flows"][2]["flow_usd"] == -484_100_000  # type: ignore[index]
    assert payload["latest_date"] == "2024-07-24"
    assert payload["laggards"][0]["ticker"] == "ETHE"  # type: ignore[index]


def test_parse_pipe_table_handles_solana_farside_rows() -> None:
    markdown = """
|  | Bitwise | VanEck | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | BSOL | VSOL | GSOL |  |
| 25 Jun 2026 | (3.9) | 0.0 | 0.0 | (3.9) |
| 26 Jun 2026 | 2.0 | - | 0.0 | 2.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("SOL", "SOL Spot ETFs", rows)

    assert rows[-1]["date"] == "2026-06-26"
    assert rows[-1]["flow_usd"] == 2_000_000
    assert payload["leaders"][0]["ticker"] == "BSOL"  # type: ignore[index]


def test_summarize_ignores_blank_current_day_placeholder() -> None:
    markdown = """
|  | Bitwise | VanEck | Grayscale | Total |
| --- | --- | --- | --- | --- |
|  | BSOL | VSOL | GSOL |  |
| 25 Jun 2026 | (3.9) | 0.0 | 0.0 | (3.9) |
| 26 Jun 2026 | 2.0 | - | 0.0 | 2.0 |
| 29 Jun 2026 | - | - | - | 0.0 |
"""

    rows = parse_pipe_table(markdown)
    payload = summarize_flow_asset("SOL", "SOL Spot ETFs", rows)

    assert rows[-1]["date"] == "2026-06-29"
    assert rows[-1]["etf_flows"] == []
    assert payload["latest_date"] == "2026-06-26"
    assert payload["latest_flow_usd"] == 2_000_000
    assert payload["five_day_flow_usd"] == -1_900_000
    assert payload["leaders"][0]["ticker"] == "BSOL"  # type: ignore[index]
    assert payload["rows"][-1]["date"] == "2026-06-26"  # type: ignore[index]
