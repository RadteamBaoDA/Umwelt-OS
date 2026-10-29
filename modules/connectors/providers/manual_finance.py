"""Strict parsers for owner-supplied personal-finance exports.

Callers must retain the resulting batch as ``local_only`` and publish it only
through the existing owner-scoped document/ingestion contracts.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

CSV_FIELDS = ("account_id", "transaction_id", "date", "amount", "currency", "description")
MAX_IMPORT_BYTES = 8 * 1024 * 1024
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class FinancialTransaction:
    """One provider-reported transaction without inferred account or money fields."""

    account_id: str
    transaction_id: str
    date: date
    amount: Decimal
    currency: str
    description: str


@dataclass(frozen=True, slots=True)
class FinancialImport:
    """Parsed personal-finance records whose owning Source must remain local-only."""

    transactions: tuple[FinancialTransaction, ...]
    local_only: Literal[True] = True


def _transaction(
    account_id: str,
    transaction_id: str,
    date_value: str | date | datetime,
    amount_value: str | Decimal,
    currency_value: str,
    description: str,
    *,
    row: int,
) -> FinancialTransaction:
    """Build one transaction from explicit values without modifying IDs or Decimal scale.

    Blank IDs, non-ISO text dates, malformed currency codes and non-finite amounts are rejected
    with a row reference. This pure helper performs no I/O and never guesses account or currency.
    """
    if not account_id.strip() or not transaction_id.strip():
        raise ValueError(f"Row {row}: account_id and transaction_id are required")
    if not _CURRENCY.fullmatch(currency_value):
        raise ValueError(f"Row {row}: currency must be an explicit three-letter uppercase code")
    try:
        if isinstance(date_value, datetime):
            transaction_date = date_value.date()
        elif isinstance(date_value, date):
            transaction_date = date_value
        else:
            raw_date = date_value.strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date) is None:
                raise ValueError("date format")
            transaction_date = date.fromisoformat(raw_date)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Row {row}: date must use ISO YYYY-MM-DD format") from exc
    try:
        amount = amount_value if isinstance(amount_value, Decimal) else Decimal(amount_value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Row {row}: amount must be a decimal number") from exc
    if not amount.is_finite():
        raise ValueError(f"Row {row}: amount must be finite")
    return FinancialTransaction(account_id, transaction_id, transaction_date, amount, currency_value, description)


def parse_finance_csv(content: bytes) -> FinancialImport:
    """Parse UTF-8 CSV bytes into local-only transactions under the fixed 8 MiB input bound.

    The header must exactly match ``CSV_FIELDS`` in order; rows retain explicit IDs, description,
    currency, date and Decimal amount. Invalid encoding, headers, rows, or empty files raise
    ``ValueError``. The parser is pure and does not persist the uploaded source bytes.
    """
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError("Personal-finance import exceeds the 8 MiB limit")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Personal-finance CSV must use UTF-8 encoding") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    fields = reader.fieldnames
    if fields is None or tuple(fields) != CSV_FIELDS:
        raise ValueError("CSV columns must be exactly: " + ", ".join(CSV_FIELDS))
    records: list[FinancialTransaction] = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(field) is None for field in CSV_FIELDS):
                raise ValueError(f"Row {row_number}: field count does not match the CSV header")
            records.append(_transaction(
                row["account_id"], row["transaction_id"], row["date"], row["amount"],
                row["currency"], row["description"], row=row_number,
            ))
    except csv.Error as exc:
        raise ValueError("Malformed personal-finance CSV") from exc
    if not records:
        raise ValueError("Personal-finance CSV contains no transactions")
    return FinancialImport(tuple(records))


def parse_finance_ofx(content: bytes) -> FinancialImport:
    """Parse bounded OFX 1/2 bytes with ofxtools and retain explicit transaction semantics.

    Every statement must supply its account and currency, and records must supply FITID, posted
    date and amount. Invalid documents raise ``ValueError``; a missing runtime dependency raises
    ``RuntimeError``. Parsing is local and leaves raw-byte persistence to no caller.
    """
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError("Personal-finance import exceeds the 8 MiB limit")
    try:
        from ofxtools.Parser import OFXTree
    except ImportError as exc:
        raise RuntimeError("OFX import requires the ofxtools package") from exc
    try:
        tree = OFXTree()
        tree.parse(io.BytesIO(content))
        document = tree.convert()
    except Exception as exc:
        raise ValueError("Invalid OFX document") from exc

    records: list[FinancialTransaction] = []
    for statement in getattr(document, "statements", ()):
        account = getattr(statement, "account", None)
        account_id = getattr(account, "acctid", None)
        currency_code = getattr(statement, "curdef", None)
        for transaction in getattr(statement, "transactions", ()):
            records.append(_transaction(
                str(account_id or ""), str(getattr(transaction, "fitid", "")),
                getattr(transaction, "dtposted", ""), getattr(transaction, "trnamt", ""),
                str(currency_code or ""),
                str(getattr(transaction, "name", None) or getattr(transaction, "memo", "")),
                row=len(records) + 1,
            ))
    if not records:
        raise ValueError("OFX document contains no supported transactions")
    return FinancialImport(tuple(records))


def parse_finance_import(filename: str, content: bytes) -> FinancialImport:
    """Select a bounded parser by the supplied filename extension; reject all other formats.

    The filename is used only to choose CSV or OFX parsing; content validation and all explicit
    field requirements remain in the selected parser. No source bytes are persisted.
    """
    suffix = filename.rpartition(".")[2].lower()
    if suffix == "csv":
        return parse_finance_csv(content)
    if suffix == "ofx":
        return parse_finance_ofx(content)
    raise ValueError("Personal-finance import supports CSV and OFX files only")


def render_finance_markdown(imported: FinancialImport, max_bytes: int = MAX_IMPORT_BYTES) -> str:
    """Render exact transaction identifiers and Decimal values in a byte-bounded Markdown table.

    Markup-sensitive cell characters are escaped for document display; values retain their
    readable content and newlines are represented explicitly. Empty batches or output exceeding
    ``max_bytes`` raise ``ValueError``. The function is pure; the caller owns local-only storage.
    """
    if max_bytes <= 0:
        raise ValueError("Markdown size limit must be positive")
    lines = [
        "# Imported financial transactions", "",
        "| Account ID | Transaction ID | Date | Amount | Currency | Description |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    size = sum(len(line.encode("utf-8")) + 1 for line in lines)
    for transaction in imported.transactions:
        values = (
            transaction.account_id, transaction.transaction_id, transaction.date.isoformat(),
            str(transaction.amount), transaction.currency, transaction.description,
        )
        cells = [
            value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "\\|").replace("\r", "\\r").replace("\n", "\\n")
            for value in values
        ]
        line = "| " + " | ".join(cells) + " |"
        size += len(line.encode("utf-8")) + 1
        if size > max_bytes:
            raise ValueError("Normalized personal-finance document exceeds the configured size limit")
        lines.append(line)
    if not imported.transactions:
        raise ValueError("Personal-finance import contains no transactions")
    return "\n".join(lines) + "\n"
