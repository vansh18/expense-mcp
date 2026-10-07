from __future__ import annotations

import calendar
import csv
from contextlib import contextmanager
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Generator

from fastmcp import FastMCP


_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = (
    _PACKAGE_ROOT
    if (_PACKAGE_ROOT / "categories.json").is_file()
    else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "expense-mcp"
)
DB_PATH = Path(os.environ.get("EXPENSE_MCP_DB_PATH", _DEFAULT_DATA_DIR / "expenses.db"))
CATEGORIES_PATH = Path(
    os.environ.get("EXPENSE_MCP_CATEGORIES_PATH", _DEFAULT_DATA_DIR / "categories.json")
)
DEFAULT_CURRENCY = os.environ.get("EXPENSE_MCP_DEFAULT_CURRENCY", "USD").upper()

mcp = FastMCP("ExpenseTracker")

_ZERO_DECIMAL_CURRENCIES = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
_THREE_DECIMAL_CURRENCIES = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_CSV_FIELDS = (
    "date",
    "amount",
    "currency",
    "transaction_type",
    "category",
    "subcategory",
    "note",
    "account",
    "payment_method",
    "reimbursable",
    "reimbursed",
)


@contextmanager
def _connect() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _currency(value: str | None) -> str:
    currency = (value or DEFAULT_CURRENCY).strip().upper()
    if not _CURRENCY_RE.fullmatch(currency):
        raise ValueError("currency must be a three-letter ISO-style currency code")
    return currency


def _minor_digits(currency: str) -> int:
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _minor_units(value: Any, currency: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("amount must be a positive decimal number")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as error:
        raise ValueError("amount must be a positive decimal number") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be finite and greater than zero")
    digits = _minor_digits(currency)
    scaled = amount * (10**digits)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{currency} amounts support at most {digits} decimal places")
    result = int(scaled)
    if result > 9_223_372_036_854_775_807:
        raise ValueError("amount exceeds SQLite's supported integer range")
    return result


def _signed_minor_units(value: Any, currency: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("opening_balance must be a finite decimal number")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as error:
        raise ValueError("opening_balance must be a finite decimal number") from error
    if not amount.is_finite():
        raise ValueError("opening_balance must be a finite decimal number")
    digits = _minor_digits(currency)
    scaled = amount * (10**digits)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{currency} amounts support at most {digits} decimal places")
    result = int(scaled)
    if not -9_223_372_036_854_775_808 <= result <= 9_223_372_036_854_775_807:
        raise ValueError("opening_balance exceeds SQLite's supported integer range")
    return result


def _amount_string(minor_units: int, currency: str) -> str:
    digits = _minor_digits(currency)
    return f"{Decimal(minor_units).scaleb(-digits):.{digits}f}"


def _validate_date(value: str, field_name: str = "date") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be an ISO date in YYYY-MM-DD format")
    return value


def _date_range(start_date: str, end_date: str) -> tuple[str, str]:
    start = _validate_date(start_date, "start_date")
    end = _validate_date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    return start, end


def _validate_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("month must use YYYY-MM format")
    year, month_number = (int(part) for part in value.split("-"))
    if not 1 <= month_number <= 12 or not 1 <= year <= 9999:
        raise ValueError("month must be a valid calendar month")
    return value


def _text(value: Any, field_name: str, max_length: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{field_name} must be no longer than {max_length} characters")
    return result


def _type(value: str) -> str:
    transaction_type = _text(value, "transaction_type", 20).lower()
    if transaction_type not in {"expense", "income"}:
        raise ValueError("transaction_type must be 'expense' or 'income'")
    return transaction_type


def _create_expenses_table(connection: sqlite3.Connection, table_name: str = "expenses") -> None:
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
            currency TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            transaction_type TEXT NOT NULL DEFAULT 'expense'
                CHECK (transaction_type IN ('expense', 'income')),
            account TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT '',
            reimbursable INTEGER NOT NULL DEFAULT 0 CHECK (reimbursable IN (0, 1)),
            reimbursed INTEGER NOT NULL DEFAULT 0 CHECK (reimbursed IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def init_db() -> None:
    """Create the schema and safely migrate the original floating-point table."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "expenses" not in tables:
            _create_expenses_table(connection)
        else:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(expenses)")
            }
            if "amount_minor" not in columns:
                legacy_rows = connection.execute(
                    "SELECT id, date, amount, category, subcategory, note FROM expenses ORDER BY id"
                ).fetchall()
                connection.execute("ALTER TABLE expenses RENAME TO expenses_legacy_migration")
                _create_expenses_table(connection)
                for row in legacy_rows:
                    currency = _currency(DEFAULT_CURRENCY)
                    minor = _minor_units(row[2], currency)
                    connection.execute(
                        """
                        INSERT INTO expenses
                            (id, date, amount_minor, currency, category, subcategory, note)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (row[0], row[1], minor, currency, row[3], row[4] or "", row[5] or ""),
                    )
                connection.execute("DROP TABLE expenses_legacy_migration")
            else:
                additions = {
                    "currency": "TEXT NOT NULL DEFAULT 'USD'",
                    "transaction_type": "TEXT NOT NULL DEFAULT 'expense'",
                    "account": "TEXT NOT NULL DEFAULT ''",
                    "payment_method": "TEXT NOT NULL DEFAULT ''",
                    "reimbursable": "INTEGER NOT NULL DEFAULT 0",
                    "reimbursed": "INTEGER NOT NULL DEFAULT 0",
                    "created_at": "TEXT NOT NULL DEFAULT ''",
                }
                for name, definition in additions.items():
                    if name not in columns:
                        connection.execute(
                            f"ALTER TABLE expenses ADD COLUMN {name} {definition}"
                        )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS expenses_date_idx ON expenses(date, id);
            CREATE INDEX IF NOT EXISTS expenses_category_date_idx ON expenses(category, date);
            CREATE INDEX IF NOT EXISTS expenses_type_date_idx ON expenses(transaction_type, date);

            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL DEFAULT '',
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                currency TEXT NOT NULL,
                UNIQUE(month, category, subcategory, currency)
            );

            CREATE TABLE IF NOT EXISTS recurring_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                next_due_date TEXT NOT NULL,
                frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly', 'yearly')),
                anchor_day INTEGER NOT NULL DEFAULT 1,
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                currency TEXT NOT NULL,
                transaction_type TEXT NOT NULL CHECK (transaction_type IN ('expense', 'income')),
                category TEXT NOT NULL,
                subcategory TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '',
                payment_method TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS recurring_occurrences (
                recurring_id INTEGER NOT NULL REFERENCES recurring_transactions(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
                PRIMARY KEY (recurring_id, due_date)
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                opening_balance_minor INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                UNIQUE(name, currency)
            );

            CREATE TABLE IF NOT EXISTS expense_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
                person TEXT NOT NULL,
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                settled INTEGER NOT NULL DEFAULT 0 CHECK (settled IN (0, 1)),
                UNIQUE(expense_id, person)
            );

            CREATE TABLE IF NOT EXISTS savings_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                target_minor INTEGER NOT NULL CHECK (target_minor > 0),
                currency TEXT NOT NULL,
                target_date TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS savings_contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL REFERENCES savings_goals(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                note TEXT NOT NULL DEFAULT ''
            );
            """
        )
        recurring_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(recurring_transactions)")
        }
        if "anchor_day" not in recurring_columns:
            connection.execute(
                "ALTER TABLE recurring_transactions ADD COLUMN anchor_day INTEGER NOT NULL DEFAULT 1"
            )


def _record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    currency = result["currency"]
    amount_minor = int(result["amount_minor"])
    result["amount_minor"] = amount_minor
    result["amount_decimal"] = _amount_string(amount_minor, currency)
    result["amount"] = float(result["amount_decimal"])
    if "reimbursable" in result:
        result["reimbursable"] = bool(result["reimbursable"])
        result["reimbursed"] = bool(result["reimbursed"])
    return result


def _insert_transaction(
    connection: sqlite3.Connection,
    transaction_date: str,
    amount_minor: int,
    currency: str,
    category: str,
    subcategory: str = "",
    note: str = "",
    transaction_type: str = "expense",
    account: str = "",
    payment_method: str = "",
    reimbursable: bool = False,
    reimbursed: bool = False,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO expenses
            (date, amount_minor, currency, category, subcategory, note,
             transaction_type, account, payment_method, reimbursable, reimbursed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_date,
            amount_minor,
            currency,
            category,
            subcategory,
            note,
            transaction_type,
            account,
            payment_method,
            int(reimbursable),
            int(reimbursed),
        ),
    )
    return int(cursor.lastrowid)


@mcp.tool()
def add_expense(
    date: str,
    amount: str | float,
    category: str,
    subcategory: str = "",
    note: str = "",
    currency: str = DEFAULT_CURRENCY,
    account: str = "",
    payment_method: str = "",
    reimbursable: bool = False,
) -> dict[str, Any]:
    """Add a validated expense. Amounts are stored exactly in integer currency minor units."""
    transaction_date = _validate_date(date)
    code = _currency(currency)
    minor = _minor_units(amount, code)
    category_name = _text(category, "category", 80)
    if not category_name:
        raise ValueError("category must not be empty")
    if not isinstance(reimbursable, bool):
        raise ValueError("reimbursable must be true or false")
    with _connect() as connection:
        expense_id = _insert_transaction(
            connection,
            transaction_date,
            minor,
            code,
            category_name,
            _text(subcategory, "subcategory", 80),
            _text(note, "note"),
            account=_text(account, "account", 100),
            payment_method=_text(payment_method, "payment_method", 100),
            reimbursable=reimbursable,
        )
    return {"status": "ok", "id": expense_id}


@mcp.tool()
def add_income(
    date: str,
    amount: str | float,
    source: str,
    note: str = "",
    currency: str = DEFAULT_CURRENCY,
    account: str = "",
    payment_method: str = "",
) -> dict[str, Any]:
    """Record income separately from spending."""
    transaction_date = _validate_date(date)
    code = _currency(currency)
    minor = _minor_units(amount, code)
    source_name = _text(source, "source", 80)
    if not source_name:
        raise ValueError("source must not be empty")
    with _connect() as connection:
        income_id = _insert_transaction(
            connection,
            transaction_date,
            minor,
            code,
            source_name,
            note=_text(note, "note"),
            transaction_type="income",
            account=_text(account, "account", 100),
            payment_method=_text(payment_method, "payment_method", 100),
        )
    return {"status": "ok", "id": income_id}


@mcp.tool()
def get_transaction(transaction_id: int) -> dict[str, Any]:
    """Look up one income or expense entry by its ID."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM expenses WHERE id = ?", (transaction_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"transaction {transaction_id} does not exist")
    return _record(row)


@mcp.tool()
def update_expense(
    expense_id: int,
    date: str | None = None,
    amount: str | float | None = None,
    currency: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    note: str | None = None,
    account: str | None = None,
    payment_method: str | None = None,
    reimbursable: bool | None = None,
    reimbursed: bool | None = None,
) -> dict[str, Any]:
    """Update supplied fields on an existing expense without changing omitted fields."""
    with _connect() as connection:
        current = connection.execute(
            "SELECT * FROM expenses WHERE id = ? AND transaction_type = 'expense'",
            (expense_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"expense {expense_id} does not exist")
        fields: dict[str, Any] = {}
        if date is not None:
            fields["date"] = _validate_date(date)
        if currency is not None or amount is not None:
            code = _currency(currency if currency is not None else current["currency"])
            if code != current["currency"] and amount is None:
                raise ValueError("amount must be supplied when changing an expense's currency")
            fields["currency"] = code
            if amount is not None:
                fields["amount_minor"] = _minor_units(amount, code)
        for field_name, value, limit in (
            ("category", category, 80),
            ("subcategory", subcategory, 80),
            ("note", note, 500),
            ("account", account, 100),
            ("payment_method", payment_method, 100),
        ):
            if value is not None:
                cleaned = _text(value, field_name, limit)
                if field_name == "category" and not cleaned:
                    raise ValueError("category must not be empty")
                fields[field_name] = cleaned
        for field_name, value in (
            ("reimbursable", reimbursable),
            ("reimbursed", reimbursed),
        ):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(f"{field_name} must be true or false")
                fields[field_name] = int(value)
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            connection.execute(
                f"UPDATE expenses SET {assignments} WHERE id = ?",
                (*fields.values(), expense_id),
            )
        row = connection.execute(
            "SELECT * FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
    return _record(row)


@mcp.tool()
def delete_expense(expense_id: int) -> dict[str, Any]:
    """Delete an expense and its associated split records."""
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM expenses WHERE id = ? AND transaction_type = 'expense'",
            (expense_id,),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"expense {expense_id} does not exist")
    return {"status": "ok", "deleted_id": expense_id}


@mcp.tool()
def delete_transaction(transaction_id: int) -> dict[str, Any]:
    """Delete an income or expense transaction by ID."""
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM expenses WHERE id = ?", (transaction_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"transaction {transaction_id} does not exist")
    return {"status": "ok", "deleted_id": transaction_id}


@mcp.tool()
def search_expenses(
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    transaction_type: str | None = "expense",
    currency: str | None = None,
    account: str | None = None,
    payment_method: str | None = None,
    note_contains: str | None = None,
    minimum_amount: str | float | None = None,
    maximum_amount: str | float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Search transactions with optional filters and bounded pagination."""
    if (start_date is None) != (end_date is None):
        raise ValueError("provide both start_date and end_date, or neither")
    filters: list[str] = []
    params: list[Any] = []
    if start_date is not None and end_date is not None:
        start, end = _date_range(start_date, end_date)
        filters.append("date BETWEEN ? AND ?")
        params.extend((start, end))
    for column, value in (
        ("category", category),
        ("subcategory", subcategory),
        ("account", account),
        ("payment_method", payment_method),
    ):
        if value is not None:
            filters.append(f"{column} = ?")
            params.append(_text(value, column, 100))
    if transaction_type is not None:
        filters.append("transaction_type = ?")
        params.append(_type(transaction_type))
    if currency is not None:
        filters.append("currency = ?")
        params.append(_currency(currency))
    if note_contains is not None:
        filters.append("instr(note, ?) > 0")
        params.append(_text(note_contains, "note_contains", 200))
    if (minimum_amount is not None or maximum_amount is not None) and currency is None:
        raise ValueError("currency must be specified when filtering by amount")
    code = _currency(currency)
    if minimum_amount is not None:
        filters.append("amount_minor >= ?")
        params.append(_minor_units(minimum_amount, code))
    if maximum_amount is not None:
        filters.append("amount_minor <= ?")
        params.append(_minor_units(maximum_amount, code))
    if minimum_amount is not None and maximum_amount is not None:
        if _minor_units(minimum_amount, code) > _minor_units(maximum_amount, code):
            raise ValueError("minimum_amount must not exceed maximum_amount")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM expenses {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [_record(row) for row in rows]


@mcp.tool()
def list_expenses(
    start_date: str,
    end_date: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List expense entries within an inclusive date range in the original insertion order."""
    start, end = _date_range(start_date, end_date)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM expenses
            WHERE date BETWEEN ? AND ? AND transaction_type = 'expense'
            ORDER BY id ASC LIMIT ? OFFSET ?
            """,
            (start, end, limit, offset),
        ).fetchall()
    return [_record(row) for row in rows]


@mcp.tool()
def summarize(
    start_date: str,
    end_date: str,
    category: str | None = None,
    currency: str | None = None,
) -> list[dict[str, Any]]:
    """Summarize expenses by category using exact integer arithmetic."""
    start, end = _date_range(start_date, end_date)
    filters = ["date BETWEEN ? AND ?", "transaction_type = 'expense'"]
    params: list[Any] = [start, end]
    if category:
        filters.append("category = ?")
        params.append(_text(category, "category", 80))
    if currency:
        filters.append("currency = ?")
        params.append(_currency(currency))
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT category, currency, SUM(amount_minor) AS total_minor,
                   COUNT(*) AS transaction_count
            FROM expenses WHERE {' AND '.join(filters)}
            GROUP BY category, currency ORDER BY category, currency
            """,
            params,
        ).fetchall()
    output = []
    for row in rows:
        minor = int(row["total_minor"])
        code = row["currency"]
        output.append(
            {
                "category": row["category"],
                "currency": code,
                "total_amount_minor": minor,
                "total_amount_decimal": _amount_string(minor, code),
                "total_amount": float(_amount_string(minor, code)),
                "transaction_count": row["transaction_count"],
            }
        )
    return output


@mcp.tool()
def summarize_cashflow(
    start_date: str, end_date: str, currency: str | None = None
) -> list[dict[str, Any]]:
    """Report income, spending, and net cash flow separately for each currency."""
    start, end = _date_range(start_date, end_date)
    filters = ["date BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if currency:
        filters.append("currency = ?")
        params.append(_currency(currency))
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT currency,
                SUM(CASE WHEN transaction_type = 'income' THEN amount_minor ELSE 0 END) AS income,
                SUM(CASE WHEN transaction_type = 'expense' THEN amount_minor ELSE 0 END) AS expenses
            FROM expenses WHERE {' AND '.join(filters)}
            GROUP BY currency ORDER BY currency
            """,
            params,
        ).fetchall()
    results = []
    for row in rows:
        code = row["currency"]
        income = int(row["income"])
        expenses = int(row["expenses"])
        net = income - expenses
        digits = _minor_digits(code)
        fmt = lambda amount: f"{Decimal(amount).scaleb(-digits):.{digits}f}"
        results.append(
            {
                "currency": code,
                "income_minor": income,
                "income_decimal": fmt(income),
                "expenses_minor": expenses,
                "expenses_decimal": fmt(expenses),
                "net_minor": net,
                "net_decimal": fmt(net),
            }
        )
    return results


@mcp.tool()
def monthly_report(year: int, month: int, currency: str | None = None) -> dict[str, Any]:
    """Summarize a calendar month by cash flow and expense category."""
    if not isinstance(year, int) or isinstance(year, bool) or not 1 <= year <= 9999:
        raise ValueError("year must be a valid four-digit year")
    if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12:
        raise ValueError("month must be an integer from 1 to 12")
    start = date(year, month, 1).isoformat()
    end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    cashflow = summarize_cashflow(start, end, currency)
    categories = summarize(start, end, currency=currency)
    return {"month": f"{year:04d}-{month:02d}", "cashflow": cashflow, "categories": categories}


@mcp.tool()
def monthly_comparison(year: int, month: int, currency: str) -> dict[str, Any]:
    """Compare expense and income totals with the previous calendar month."""
    if not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("month must be an integer from 1 to 12")
    current = monthly_report(year, month, currency)
    previous_year, previous_month = (year - 1, 12) if month == 1 else (year, month - 1)
    previous = monthly_report(previous_year, previous_month, currency)
    current_flow = current["cashflow"]
    previous_flow = previous["cashflow"]
    if not current_flow:
        current_flow = [{"currency": _currency(currency), "income_minor": 0, "expenses_minor": 0, "net_minor": 0}]
    if not previous_flow:
        previous_flow = [{"currency": _currency(currency), "income_minor": 0, "expenses_minor": 0, "net_minor": 0}]
    now = current_flow[0]
    before = previous_flow[0]
    code = _currency(currency)
    return {
        "current_month": current["month"],
        "previous_month": previous["month"],
        "currency": code,
        "income_change_minor": now["income_minor"] - before["income_minor"],
        "expense_change_minor": now["expenses_minor"] - before["expenses_minor"],
        "net_change_minor": now["net_minor"] - before["net_minor"],
        "current_categories": current["categories"],
        "previous_categories": previous["categories"],
    }


@mcp.tool()
def set_budget(
    month: str,
    category: str,
    amount: str | float,
    currency: str = DEFAULT_CURRENCY,
    subcategory: str = "",
) -> dict[str, Any]:
    """Create or replace a monthly budget for a category and optional subcategory."""
    month = _validate_month(month)
    category_name = _text(category, "category", 80)
    if not category_name:
        raise ValueError("category must not be empty")
    code = _currency(currency)
    minor = _minor_units(amount, code)
    subcategory_name = _text(subcategory, "subcategory", 80)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO budgets(month, category, subcategory, amount_minor, currency)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(month, category, subcategory, currency)
            DO UPDATE SET amount_minor = excluded.amount_minor
            """,
            (month, category_name, subcategory_name, minor, code),
        )
        row = connection.execute(
            """
            SELECT * FROM budgets
            WHERE month = ? AND category = ? AND subcategory = ? AND currency = ?
            """,
            (month, category_name, subcategory_name, code),
        ).fetchone()
    return {
        "id": row["id"],
        "month": row["month"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "currency": code,
        "amount_minor": minor,
        "amount_decimal": _amount_string(minor, code),
    }


@mcp.tool()
def budget_vs_actual(month: str, currency: str | None = None) -> list[dict[str, Any]]:
    """Compare monthly budgets to matching expense totals, separately by currency."""
    month = _validate_month(month)
    year, month_number = (int(part) for part in month.split("-"))
    start = f"{month}-01"
    end = date(year, month_number, calendar.monthrange(year, month_number)[1]).isoformat()
    with _connect() as connection:
        filters = ["month = ?"]
        params: list[Any] = [month]
        if currency:
            filters.append("currency = ?")
            params.append(_currency(currency))
        budgets = connection.execute(
            f"SELECT * FROM budgets WHERE {' AND '.join(filters)} ORDER BY category, subcategory, currency",
            params,
        ).fetchall()
        output = []
        for budget in budgets:
            actual = connection.execute(
                """
                SELECT COALESCE(SUM(amount_minor), 0)
                FROM expenses
                WHERE date BETWEEN ? AND ? AND transaction_type = 'expense'
                  AND category = ? AND currency = ?
                  AND (? = '' OR subcategory = ?)
                """,
                (
                    start,
                    end,
                    budget["category"],
                    budget["currency"],
                    budget["subcategory"],
                    budget["subcategory"],
                ),
            ).fetchone()[0]
            planned = int(budget["amount_minor"])
            spent = int(actual)
            code = budget["currency"]
            remaining = planned - spent
            output.append(
                {
                    "month": month,
                    "category": budget["category"],
                    "subcategory": budget["subcategory"],
                    "currency": code,
                    "budget_minor": planned,
                    "budget_decimal": _amount_string(planned, code),
                    "actual_minor": spent,
                    "actual_decimal": _amount_string(spent, code),
                    "remaining_minor": remaining,
                    "remaining_decimal": _amount_string(remaining, code),
                    "percent_used": round(spent * 100 / planned, 2),
                }
            )
    return output


@mcp.tool()
def delete_budget(
    month: str,
    category: str,
    currency: str = DEFAULT_CURRENCY,
    subcategory: str = "",
) -> dict[str, Any]:
    """Delete one monthly category or subcategory budget."""
    month = _validate_month(month)
    code = _currency(currency)
    with _connect() as connection:
        cursor = connection.execute(
            """
            DELETE FROM budgets
            WHERE month = ? AND category = ? AND subcategory = ? AND currency = ?
            """,
            (
                month,
                _text(category, "category", 80),
                _text(subcategory, "subcategory", 80),
                code,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("matching budget does not exist")
    return {"status": "ok", "month": month, "category": category, "currency": code}


def _next_due(value: date, frequency: str, anchor_day: int | None = None) -> date:
    if frequency == "daily":
        return value + timedelta(days=1)
    if frequency == "weekly":
        return value + timedelta(days=7)
    if frequency == "monthly":
        year = value.year + (1 if value.month == 12 else 0)
        month = 1 if value.month == 12 else value.month + 1
        return date(
            year,
            month,
            min(anchor_day or value.day, calendar.monthrange(year, month)[1]),
        )
    year = value.year + 1
    return date(
        year,
        value.month,
        min(anchor_day or value.day, calendar.monthrange(year, value.month)[1]),
    )


@mcp.tool()
def create_recurring_transaction(
    next_due_date: str,
    frequency: str,
    amount: str | float,
    category: str,
    transaction_type: str = "expense",
    currency: str = DEFAULT_CURRENCY,
    subcategory: str = "",
    note: str = "",
    account: str = "",
    payment_method: str = "",
) -> dict[str, Any]:
    """Define a recurring expense or income; due entries are recorded only on request."""
    due = _validate_date(next_due_date, "next_due_date")
    cadence = _text(frequency, "frequency", 20).lower()
    if cadence not in {"daily", "weekly", "monthly", "yearly"}:
        raise ValueError("frequency must be daily, weekly, monthly, or yearly")
    code = _currency(currency)
    minor = _minor_units(amount, code)
    kind = _type(transaction_type)
    category_name = _text(category, "category", 80)
    if not category_name:
        raise ValueError("category must not be empty")
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO recurring_transactions
                (next_due_date, frequency, anchor_day, amount_minor, currency, transaction_type,
                 category, subcategory, note, account, payment_method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                due,
                cadence,
                date.fromisoformat(due).day,
                minor,
                code,
                kind,
                category_name,
                _text(subcategory, "subcategory", 80),
                _text(note, "note"),
                _text(account, "account", 100),
                _text(payment_method, "payment_method", 100),
            ),
        )
    return {"status": "ok", "id": int(cursor.lastrowid), "next_due_date": due}


@mcp.tool()
def list_recurring_transactions(include_inactive: bool = False) -> list[dict[str, Any]]:
    """List recurring transaction definitions."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM recurring_transactions "
            + ("" if include_inactive else "WHERE active = 1 ")
            + "ORDER BY next_due_date, id"
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["amount_minor"] = int(item["amount_minor"])
        item["amount_decimal"] = _amount_string(item["amount_minor"], item["currency"])
        item["active"] = bool(item["active"])
        output.append(item)
    return output


@mcp.tool()
def generate_due_transactions(through_date: str) -> dict[str, Any]:
    """Record each due recurring item once, up to and including through_date."""
    cutoff = _validate_date(through_date, "through_date")
    created: list[dict[str, Any]] = []
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM recurring_transactions WHERE active = 1 ORDER BY id"
        ).fetchall()
        for recurring in rows:
            due = date.fromisoformat(recurring["next_due_date"])
            while due.isoformat() <= cutoff:
                if len(created) >= 10_000:
                    raise ValueError(
                        "recurrence generation is limited to 10,000 transactions per call"
                    )
                due_text = due.isoformat()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO expenses
                        (date, amount_minor, currency, category, subcategory, note,
                         transaction_type, account, payment_method)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        due_text,
                        recurring["amount_minor"],
                        recurring["currency"],
                        recurring["category"],
                        recurring["subcategory"],
                        recurring["note"],
                        recurring["transaction_type"],
                        recurring["account"],
                        recurring["payment_method"],
                    ),
                )
                if cursor.rowcount:
                    transaction_id = int(cursor.lastrowid)
                    connection.execute(
                        """
                        INSERT INTO recurring_occurrences(recurring_id, due_date, expense_id)
                        VALUES (?, ?, ?)
                        """,
                        (recurring["id"], due_text, transaction_id),
                    )
                    created.append({"recurring_id": recurring["id"], "date": due_text, "id": transaction_id})
                due = _next_due(
                    due, recurring["frequency"], recurring["anchor_day"]
                )
            connection.execute(
                "UPDATE recurring_transactions SET next_due_date = ? WHERE id = ?",
                (due.isoformat(), recurring["id"]),
            )
    return {"created_count": len(created), "transactions": created}


@mcp.tool()
def set_recurring_active(recurring_id: int, active: bool) -> dict[str, Any]:
    """Pause or resume a recurring transaction definition."""
    if not isinstance(active, bool):
        raise ValueError("active must be true or false")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE recurring_transactions SET active = ? WHERE id = ?",
            (int(active), recurring_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"recurring transaction {recurring_id} does not exist")
    return {"id": recurring_id, "active": active}


@mcp.tool()
def create_account(
    name: str,
    account_type: str,
    currency: str = DEFAULT_CURRENCY,
    opening_balance: str | float = 0,
) -> dict[str, Any]:
    """Create a local account or payment source with an optional opening balance."""
    label = _text(name, "name", 100)
    kind = _text(account_type, "account_type", 50)
    if not label or not kind:
        raise ValueError("name and account_type must not be empty")
    code = _currency(currency)
    balance = _signed_minor_units(opening_balance, code)
    with _connect() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO accounts(name, account_type, currency, opening_balance_minor)
                VALUES (?, ?, ?, ?)
                """,
                (label, kind, code, balance),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("an account with this name and currency already exists") from error
    return {"id": int(cursor.lastrowid), "name": label, "account_type": kind, "currency": code}


@mcp.tool()
def list_accounts(include_inactive: bool = False) -> list[dict[str, Any]]:
    """List local accounts and payment sources."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM accounts "
            + ("" if include_inactive else "WHERE active = 1 ")
            + "ORDER BY name, currency"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["opening_balance_decimal"] = _amount_string(
            item["opening_balance_minor"], item["currency"]
        )
        item["active"] = bool(item["active"])
        result.append(item)
    return result


@mcp.tool()
def account_balances(include_inactive: bool = False) -> list[dict[str, Any]]:
    """Calculate each account's balance from its opening balance and tagged transactions."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT a.id, a.name, a.account_type, a.currency, a.active,
                   a.opening_balance_minor
                     + COALESCE(SUM(CASE
                         WHEN e.transaction_type = 'income' THEN e.amount_minor
                         WHEN e.transaction_type = 'expense' THEN -e.amount_minor
                         ELSE 0 END), 0) AS balance_minor
            FROM accounts a
            LEFT JOIN expenses e ON e.account = a.name AND e.currency = a.currency
            """
            + ("" if include_inactive else "WHERE a.active = 1 ")
            + "GROUP BY a.id ORDER BY a.name, a.currency"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "account_type": row["account_type"],
            "currency": row["currency"],
            "active": bool(row["active"]),
            "balance_minor": row["balance_minor"],
            "balance_decimal": _amount_string(row["balance_minor"], row["currency"]),
        }
        for row in rows
    ]


@mcp.tool()
def set_account_active(account_id: int, active: bool) -> dict[str, Any]:
    """Archive or reactivate an account without deleting transaction history."""
    if not isinstance(active, bool):
        raise ValueError("active must be true or false")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE accounts SET active = ? WHERE id = ?", (int(active), account_id)
        )
        if cursor.rowcount == 0:
            raise ValueError(f"account {account_id} does not exist")
    return {"id": account_id, "active": active}


@mcp.tool()
def set_expense_splits(expense_id: int, splits: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the people/shares for an expense; all shares together cannot exceed its cost."""
    if not isinstance(splits, list):
        raise ValueError("splits must be a list of {person, amount} objects")
    with _connect() as connection:
        expense = connection.execute(
            "SELECT * FROM expenses WHERE id = ? AND transaction_type = 'expense'",
            (expense_id,),
        ).fetchone()
        if expense is None:
            raise ValueError(f"expense {expense_id} does not exist")
        normalized: list[tuple[str, int]] = []
        names = set()
        for split in splits:
            if not isinstance(split, dict) or "person" not in split or "amount" not in split:
                raise ValueError("each split must include person and amount")
            person = _text(split["person"], "person", 100)
            if not person or person.casefold() in names:
                raise ValueError("split people must be non-empty and unique")
            names.add(person.casefold())
            normalized.append(
                (person, _minor_units(split["amount"], expense["currency"]))
            )
        if sum(amount for _, amount in normalized) > expense["amount_minor"]:
            raise ValueError("the sum of shares cannot exceed the expense amount")
        connection.execute("DELETE FROM expense_splits WHERE expense_id = ?", (expense_id,))
        connection.executemany(
            "INSERT INTO expense_splits(expense_id, person, amount_minor) VALUES (?, ?, ?)",
            [(expense_id, person, amount) for person, amount in normalized],
        )
        rows = connection.execute(
            "SELECT * FROM expense_splits WHERE expense_id = ? ORDER BY person",
            (expense_id,),
        ).fetchall()
    return {
        "expense_id": expense_id,
        "currency": expense["currency"],
        "splits": [
            {
                "person": row["person"],
                "amount_minor": row["amount_minor"],
                "amount_decimal": _amount_string(row["amount_minor"], expense["currency"]),
                "settled": bool(row["settled"]),
            }
            for row in rows
        ],
    }


@mcp.tool()
def list_expense_splits(expense_id: int) -> list[dict[str, Any]]:
    """List who owes what for an expense."""
    with _connect() as connection:
        expense = connection.execute(
            "SELECT currency FROM expenses WHERE id = ? AND transaction_type = 'expense'",
            (expense_id,),
        ).fetchone()
        if expense is None:
            raise ValueError(f"expense {expense_id} does not exist")
        rows = connection.execute(
            "SELECT * FROM expense_splits WHERE expense_id = ? ORDER BY person",
            (expense_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "person": row["person"],
            "amount_minor": row["amount_minor"],
            "amount_decimal": _amount_string(row["amount_minor"], expense["currency"]),
            "currency": expense["currency"],
            "settled": bool(row["settled"]),
        }
        for row in rows
    ]


@mcp.tool()
def settle_expense_split(split_id: int, settled: bool = True) -> dict[str, Any]:
    """Mark a person's share of an expense as paid or unpaid."""
    if not isinstance(settled, bool):
        raise ValueError("settled must be true or false")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE expense_splits SET settled = ? WHERE id = ?",
            (int(settled), split_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"expense split {split_id} does not exist")
    return {"id": split_id, "settled": settled}


@mcp.tool()
def create_savings_goal(
    name: str,
    target_amount: str | float,
    currency: str = DEFAULT_CURRENCY,
    target_date: str = "",
) -> dict[str, Any]:
    """Create a savings goal; contributions are tracked separately from expenses."""
    label = _text(name, "name", 100)
    if not label:
        raise ValueError("name must not be empty")
    code = _currency(currency)
    target = _minor_units(target_amount, code)
    due = _validate_date(target_date, "target_date") if target_date else ""
    with _connect() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO savings_goals(name, target_minor, currency, target_date)
                VALUES (?, ?, ?, ?)
                """,
                (label, target, code, due),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("a savings goal with this name already exists") from error
    return {"id": int(cursor.lastrowid), "name": label, "target_amount_decimal": _amount_string(target, code)}


@mcp.tool()
def contribute_to_savings_goal(
    goal_id: int, date: str, amount: str | float, note: str = ""
) -> dict[str, Any]:
    """Add a dated contribution to an existing savings goal."""
    when = _validate_date(date)
    with _connect() as connection:
        goal = connection.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND active = 1", (goal_id,)
        ).fetchone()
        if goal is None:
            raise ValueError(f"active savings goal {goal_id} does not exist")
        minor = _minor_units(amount, goal["currency"])
        cursor = connection.execute(
            """
            INSERT INTO savings_contributions(goal_id, date, amount_minor, note)
            VALUES (?, ?, ?, ?)
            """,
            (goal_id, when, minor, _text(note, "note")),
        )
    return {"id": int(cursor.lastrowid), "goal_id": goal_id, "amount_decimal": _amount_string(minor, goal["currency"])}


@mcp.tool()
def list_savings_goals(include_inactive: bool = False) -> list[dict[str, Any]]:
    """List goals with exact contribution totals and progress percentages."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT g.*, COALESCE(SUM(c.amount_minor), 0) AS saved_minor
            FROM savings_goals g LEFT JOIN savings_contributions c ON c.goal_id = g.id
            """
            + ("" if include_inactive else "WHERE g.active = 1 ")
            + "GROUP BY g.id ORDER BY g.name"
        ).fetchall()
    result = []
    for row in rows:
        target = int(row["target_minor"])
        saved = int(row["saved_minor"])
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "currency": row["currency"],
                "target_date": row["target_date"],
                "active": bool(row["active"]),
                "target_minor": target,
                "target_decimal": _amount_string(target, row["currency"]),
                "saved_minor": saved,
                "saved_decimal": _amount_string(saved, row["currency"]),
                "remaining_minor": target - saved,
                "progress_percent": round(saved * 100 / target, 2),
            }
        )
    return result


@mcp.tool()
def set_savings_goal_active(goal_id: int, active: bool) -> dict[str, Any]:
    """Archive or reactivate a savings goal without deleting its contribution history."""
    if not isinstance(active, bool):
        raise ValueError("active must be true or false")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE savings_goals SET active = ? WHERE id = ?",
            (int(active), goal_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"savings goal {goal_id} does not exist")
    return {"id": goal_id, "active": active}


@mcp.tool()
def export_expenses(
    start_date: str, end_date: str, transaction_type: str | None = None, currency: str | None = None
) -> str:
    """Export a date range as CSV text for user-controlled saving."""
    start, end = _date_range(start_date, end_date)
    filters = ["date BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if transaction_type is not None:
        filters.append("transaction_type = ?")
        params.append(_type(transaction_type))
    if currency is not None:
        filters.append("currency = ?")
        params.append(_currency(currency))
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM expenses WHERE {' AND '.join(filters)} ORDER BY date, id",
            params,
        ).fetchall()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        item = _record(row)
        writer.writerow(
            {
                "date": item["date"],
                "amount": item["amount_decimal"],
                "currency": item["currency"],
                "transaction_type": item["transaction_type"],
                "category": item["category"],
                "subcategory": item["subcategory"],
                "note": item["note"],
                "account": item["account"],
                "payment_method": item["payment_method"],
                "reimbursable": str(item["reimbursable"]).lower(),
                "reimbursed": str(item["reimbursed"]).lower(),
            }
        )
    return output.getvalue()


@mcp.tool()
def import_expenses(csv_content: str, duplicate_policy: str = "skip") -> dict[str, int]:
    """Validate and atomically import CSV text; exact duplicate rows are skipped by default."""
    if not isinstance(csv_content, str) or len(csv_content) > 5_000_000:
        raise ValueError("csv_content must be text no larger than 5 MB")
    if duplicate_policy not in {"skip", "add", "error"}:
        raise ValueError("duplicate_policy must be skip, add, or error")
    reader = csv.DictReader(io.StringIO(csv_content))
    if reader.fieldnames is None or not {"date", "amount", "category"}.issubset(reader.fieldnames):
        raise ValueError("CSV must include date, amount, and category columns")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"CSV row {line_number} has extra columns")
            code = _currency(row.get("currency") or DEFAULT_CURRENCY)
            kind = _type(row.get("transaction_type") or "expense")
            when = _validate_date(row.get("date", ""), f"row {line_number} date")
            category = _text(row.get("category"), "category", 80)
            if not category:
                raise ValueError(f"CSV row {line_number} category must not be empty")
            reimbursable = (row.get("reimbursable") or "false").strip().lower()
            reimbursed = (row.get("reimbursed") or "false").strip().lower()
            if reimbursable not in {"true", "false", "1", "0"} or reimbursed not in {"true", "false", "1", "0"}:
                raise ValueError(f"CSV row {line_number} boolean fields must be true or false")
            rows.append(
                {
                    "date": when,
                    "amount_minor": _minor_units(row.get("amount"), code),
                    "currency": code,
                    "transaction_type": kind,
                    "category": category,
                    "subcategory": _text(row.get("subcategory"), "subcategory", 80),
                    "note": _text(row.get("note"), "note"),
                    "account": _text(row.get("account"), "account", 100),
                    "payment_method": _text(row.get("payment_method"), "payment_method", 100),
                    "reimbursable": int(reimbursable in {"true", "1"}),
                    "reimbursed": int(reimbursed in {"true", "1"}),
                }
            )
            if len(rows) > 10_000:
                raise ValueError("CSV import is limited to 10,000 transactions per call")
    except csv.Error as error:
        raise ValueError(f"invalid CSV: {error}") from error
    inserted = skipped = 0
    with _connect() as connection:
        for row in rows:
            same = connection.execute(
                """
                SELECT id FROM expenses
                WHERE date = ? AND amount_minor = ? AND currency = ?
                  AND transaction_type = ? AND category = ? AND subcategory = ?
                  AND note = ? AND account = ? AND payment_method = ?
                  AND reimbursable = ? AND reimbursed = ?
                LIMIT 1
                """,
                (
                    row["date"],
                    row["amount_minor"],
                    row["currency"],
                    row["transaction_type"],
                    row["category"],
                    row["subcategory"],
                    row["note"],
                    row["account"],
                    row["payment_method"],
                    row["reimbursable"],
                    row["reimbursed"],
                ),
            ).fetchone()
            if same and duplicate_policy == "error":
                raise ValueError(f"CSV contains a transaction already in the database (ID {same['id']})")
            if same and duplicate_policy == "skip":
                skipped += 1
                continue
            _insert_transaction(connection, **row)
            inserted += 1
    return {"inserted": inserted, "skipped_duplicates": skipped}


def _backup_directory() -> Path:
    directory = Path(os.environ.get("EXPENSE_MCP_BACKUP_DIR", DB_PATH.parent / "backups"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _backup_path(backup_name: str) -> Path:
    name = _text(backup_name, "backup_name", 100)
    if (
        not _BACKUP_NAME_RE.fullmatch(name)
        or name.startswith(".")
        or ".." in name
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("backup_name must be a simple filename without path components")
    if not name.endswith(".sqlite3"):
        name += ".sqlite3"
    destination = _backup_directory() / name
    if destination.is_symlink() or destination.resolve().parent != _backup_directory():
        raise ValueError("backup path must be a regular file within the configured backup directory")
    return destination


@mcp.tool()
def backup_database(backup_name: str) -> dict[str, str]:
    """Create a consistent SQLite backup in the configured local backups directory."""
    destination_path = _backup_path(backup_name)
    if destination_path.resolve() == DB_PATH.resolve():
        raise ValueError("backup destination must differ from the active database")
    with _connect() as source, sqlite3.connect(destination_path) as destination:
        source.backup(destination)
    return {"status": "ok", "backup_name": destination_path.name, "path": str(destination_path)}


@mcp.tool()
def list_database_backups() -> list[dict[str, Any]]:
    """List SQLite backup files stored in the configured backups directory."""
    output = []
    for path in sorted(_backup_directory().glob("*.sqlite3")):
        output.append({"backup_name": path.name, "size_bytes": path.stat().st_size})
    return output


@mcp.tool()
def restore_database_backup(backup_name: str) -> dict[str, str]:
    """Restore a selected local backup after verifying SQLite integrity and schema."""
    source_path = _backup_path(backup_name)
    if not source_path.is_file():
        raise ValueError(f"backup {source_path.name} does not exist")
    if source_path.resolve() == DB_PATH.resolve():
        raise ValueError("cannot restore the active database file as its own backup")
    with sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True) as source:
        check = source.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise ValueError("backup failed SQLite integrity_check")
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = (
            {row[1] for row in source.execute("PRAGMA table_info(expenses)")}
            if "expenses" in tables
            else set()
        )
        required_columns = {
            "id",
            "date",
            "amount_minor",
            "currency",
            "category",
            "subcategory",
            "note",
            "transaction_type",
            "account",
            "payment_method",
            "reimbursable",
            "reimbursed",
            "created_at",
        }
        required_tables = {
            "budgets",
            "recurring_transactions",
            "recurring_occurrences",
            "accounts",
            "expense_splits",
            "savings_goals",
            "savings_contributions",
        }
        if not required_columns.issubset(columns) or not required_tables.issubset(tables):
            raise ValueError("backup does not contain a supported expense database")
        safety_copy = backup_database(
            f"pre-restore-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.sqlite3"
        )
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as destination:
            source.backup(destination)
    return {
        "status": "ok",
        "restored_backup": source_path.name,
        "safety_backup": safety_copy["backup_name"],
    }


@mcp.prompt()
def monthly_spending_review(month: str) -> str:
    """Help interpret a month of locally tracked spending."""
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must use YYYY-MM format")
    return (
        f"Review expense spending for {month}. Call budget_vs_actual for that month, "
        "then summarize_cashflow for its date range. Report each currency separately, "
        "highlight overspent budgets and the largest category totals, and do not invent transactions."
    )


@mcp.prompt()
def unusual_expense_review(start_date: str, end_date: str) -> str:
    """Guide a grounded review for unusually large or potentially duplicate entries."""
    start, end = _date_range(start_date, end_date)
    return (
        f"Review transactions from {start} through {end}. Use search_expenses and "
        "summarize to identify unusually large expenses or exact-looking duplicates. "
        "Compare only transactions in the same currency and date range; show IDs and "
        "ask before editing or deleting anything."
    )


@mcp.resource("expense://categories", mime_type="application/json")
def categories() -> str:
    """Return category suggestions, re-reading the editable JSON file each time."""
    with CATEGORIES_PATH.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("categories.json must contain a JSON object")
    return json.dumps(value, ensure_ascii=False)


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
