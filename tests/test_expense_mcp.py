import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
import asyncio

from fastmcp import Client
from expense_mcp import server


class ExpenseMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "expenses.sqlite3"
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def add_sample(self, amount="12.34", category="Food", date="2026-10-06", **kwargs):
        return server.add_expense(date, amount, category, **kwargs)["id"]

    def test_money_is_stored_and_summed_as_minor_units(self):
        self.add_sample("0.10", "Dining")
        self.add_sample("0.20", "Dining")

        rows = server.summarize("2026-10-01", "2026-10-31")

        self.assertEqual(rows[0]["total_amount_minor"], 30)
        self.assertEqual(rows[0]["total_amount_decimal"], "0.30")
        self.assertEqual(server.get_transaction(1)["amount_decimal"], "0.10")

    def test_currency_precision_and_input_validation(self):
        expense_id = server.add_expense(
            "2026-10-01", "125", "Transit", currency="JPY"
        )["id"]
        self.assertEqual(server.get_transaction(expense_id)["amount_decimal"], "125")

        for date_value, amount in (("2026-02-30", "1"), ("2026-01-01", "1.001")):
            with self.subTest(date=date_value, amount=amount):
                with self.assertRaises(ValueError):
                    server.add_expense(date_value, amount, "Food")
        with self.assertRaises(ValueError):
            server.add_expense("2026-01-01", "NaN", "Food")
        with self.assertRaises(ValueError):
            server.add_expense("2026-01-01", "0", "Food")

    def test_migrates_legacy_expenses_and_preserves_ids(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT INTO expenses(id, date, amount, category, subcategory, note) "
                "VALUES (42, '2026-01-02', 19.95, 'Legacy Category', 'Old', 'kept')"
            )
        server.DB_PATH = legacy_path
        server.init_db()

        row = server.get_transaction(42)

        self.assertEqual(row["amount_minor"], 1995)
        self.assertEqual(row["amount_decimal"], "19.95")
        self.assertEqual(row["category"], "Legacy Category")
        self.assertEqual(row["note"], "kept")

    def test_income_cashflow_and_month_comparison(self):
        self.add_sample("5.25", date="2026-10-03")
        server.add_income("2026-10-04", "20.00", "Pay", currency="USD")
        server.add_expense("2026-09-04", "3.25", "Food")

        flow = server.summarize_cashflow("2026-10-01", "2026-10-31", "USD")[0]
        comparison = server.monthly_comparison(2026, 10, "USD")

        self.assertEqual(flow["net_minor"], 1475)
        self.assertEqual(comparison["expense_change_minor"], 200)

    def test_search_pagination_and_amount_filters_require_currency(self):
        self.add_sample("1.00", "Food", "2026-10-01")
        self.add_sample("2.00", "Food", "2026-10-02")
        self.add_sample("3.00", "Food", "2026-10-03")

        first = server.search_expenses(category="Food", limit=2, offset=0)
        second = server.search_expenses(category="Food", limit=2, offset=2)
        self.assertEqual([row["amount_decimal"] for row in first], ["3.00", "2.00"])
        self.assertEqual([row["amount_decimal"] for row in second], ["1.00"])
        with self.assertRaisesRegex(ValueError, "currency must be specified"):
            server.search_expenses(minimum_amount="1.00")

    def test_list_expenses_preserves_original_insertion_order(self):
        first_id = self.add_sample("1.00", "Food", "2026-10-02")
        second_id = self.add_sample("2.00", "Food", "2026-10-01")

        rows = server.list_expenses("2026-10-01", "2026-10-31")

        self.assertEqual([row["id"] for row in rows], [first_id, second_id])

    def test_update_delete_and_reimbursement(self):
        expense_id = self.add_sample("12.34", reimbursable=True)
        updated = server.update_expense(
            expense_id, amount="10.99", category="Health", reimbursed=True
        )
        self.assertEqual(updated["amount_minor"], 1099)
        self.assertTrue(updated["reimbursable"])
        self.assertTrue(updated["reimbursed"])
        self.assertEqual(server.delete_expense(expense_id)["deleted_id"], expense_id)
        with self.assertRaises(ValueError):
            server.get_transaction(expense_id)
        expense_id = self.add_sample("12.34")
        with self.assertRaisesRegex(ValueError, "amount must be supplied"):
            server.update_expense(expense_id, currency="EUR")

    def test_budget_category_matches_subcategories(self):
        self.add_sample("8.50", "Food", subcategory="Groceries")
        self.add_sample("1.50", "Food", subcategory="Dining")
        server.set_budget("2026-10", "Food", "9.00")

        result = server.budget_vs_actual("2026-10")[0]

        self.assertEqual(result["actual_minor"], 1000)
        self.assertEqual(result["remaining_minor"], -100)
        self.assertEqual(result["percent_used"], 111.11)

    def test_recurring_monthly_date_anchor_and_no_duplicate_occurrences(self):
        recurring_id = server.create_recurring_transaction(
            "2026-01-31", "monthly", "10.00", "Rent"
        )["id"]

        generated = server.generate_due_transactions("2026-03-31")
        repeated = server.generate_due_transactions("2026-03-31")
        template = next(
            row for row in server.list_recurring_transactions() if row["id"] == recurring_id
        )

        self.assertEqual(
            [item["date"] for item in generated["transactions"]],
            ["2026-01-31", "2026-02-28", "2026-03-31"],
        )
        self.assertEqual(repeated["created_count"], 0)
        self.assertEqual(template["next_due_date"], "2026-04-30")

    def test_accounts_splits_and_savings_goals(self):
        account = server.create_account("Everyday", "checking", opening_balance="-25.50")
        self.assertEqual(server.list_accounts()[0]["opening_balance_decimal"], "-25.50")

        expense_id = self.add_sample("10.00", account="Everyday")
        server.add_income("2026-10-06", "50.00", "Pay", account="Everyday")
        self.assertEqual(server.account_balances()[0]["balance_decimal"], "14.50")
        result = server.set_expense_splits(
            expense_id,
            [{"person": "Alex", "amount": "4.25"}, {"person": "Sam", "amount": "2.00"}],
        )
        split_id = server.list_expense_splits(expense_id)[0]["id"]
        server.settle_expense_split(split_id)
        self.assertEqual(len(result["splits"]), 2)
        with self.assertRaises(ValueError):
            server.set_expense_splits(expense_id, [{"person": "Alex", "amount": "11.00"}])

        goal_id = server.create_savings_goal("Trip", "1000")["id"]
        server.contribute_to_savings_goal(goal_id, "2026-10-01", "25.50")
        goal = server.list_savings_goals()[0]
        self.assertEqual(goal["saved_minor"], 2550)
        self.assertEqual(account["name"], "Everyday")

    def test_csv_export_import_is_exact_and_invalid_import_is_atomic(self):
        self.add_sample("12.34", "Food", note="lunch")
        csv_text = server.export_expenses("2026-10-01", "2026-10-31")

        result = server.import_expenses(csv_text)
        self.assertEqual(result, {"inserted": 0, "skipped_duplicates": 1})
        parsed = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(parsed[0]["amount"], "12.34")

        invalid = (
            "date,amount,category\n"
            "2026-10-01,4.00,Transport\n"
            "2026-02-30,5.00,Food\n"
        )
        with self.assertRaises(ValueError):
            server.import_expenses(invalid)
        rows = server.search_expenses(category="Transport")
        self.assertEqual(rows, [])

    def test_backup_restore_creates_safety_copy(self):
        self.add_sample("7.00", "Food")
        backup = server.backup_database("before-change")
        self.add_sample("9.00", "Travel", date="2026-10-07")

        result = server.restore_database_backup(backup["backup_name"])

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["safety_backup"].startswith("pre-restore-"))
        self.assertEqual(len(server.search_expenses()), 1)
        self.assertEqual(len(server.list_database_backups()), 2)

    def test_mcp_tools_prompts_and_categories_resource_are_registered(self):
        async def inspect_server():
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
                prompts = await client.list_prompts()
                resources = await client.list_resources()
                return (
                    {tool.name for tool in tools},
                    {prompt.name for prompt in prompts},
                    {str(resource.uri) for resource in resources},
                )

        tools, prompts, resources = asyncio.run(inspect_server())

        self.assertIn("add_expense", tools)
        self.assertIn("budget_vs_actual", tools)
        self.assertIn("restore_database_backup", tools)
        self.assertIn("monthly_spending_review", prompts)
        self.assertIn("unusual_expense_review", prompts)
        self.assertIn("expense://categories", resources)


if __name__ == "__main__":
    unittest.main()
