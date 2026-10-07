# Expense MCP

A personal, local-first expense tracker exposed as an MCP server. Transactions and related records stay in SQLite on the local machine; there is no bank connection, remote service, or multi-user authentication.

## Run

Install the project with `uv sync`, then start it with `uv run expense-mcp`. For direct development use, `uv run python main.py` is also supported. The package entry point and repository script both start the same MCP server.

By default, the database is `expenses.db` beside the repository's `categories.json`. For a different location, set `EXPENSE_MCP_DB_PATH`. Set `EXPENSE_MCP_CATEGORIES_PATH` to use a different category JSON file and `EXPENSE_MCP_DEFAULT_CURRENCY` to change the default currency (USD by default). Backups are written under `backups` beside the active database; `EXPENSE_MCP_BACKUP_DIR` can select another local backup directory.

The first server startup migrates the original `expenses(date, amount REAL, category, subcategory, note)` table in place. Existing IDs, dates, categories, notes, and exact cent amounts are retained. Legacy amounts are interpreted in the configured default currency. If migration encounters a value that cannot be represented exactly, startup fails rather than silently rounding it.

## Use with Claude Desktop

Claude Desktop can launch this MCP server locally over standard input/output:

1. Install `uv` and run `uv sync` from the project directory.
2. Open Claude Desktop's MCP configuration file:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. Add the server entry below, replacing the example project path with the absolute path to this repository:

   ```json
   {
     "mcpServers": {
       "expense-mcp": {
         "command": "uv",
         "args": [
           "--directory",
           "C:\\path\\to\\expense-mcp",
           "run",
           "expense-mcp"
         ]
       }
     }
   }
   ```

   If the configuration already contains other MCP servers, add only the `"expense-mcp"` entry inside its existing `"mcpServers"` object. On macOS, use a path such as `"/Users/your-name/expense-mcp"` instead.
4. Save the file and restart Claude Desktop. The expense tools and prompts should then be available in Claude.

The server and its SQLite database run on your machine; Claude launches the server when needed. If Claude cannot find `uv`, use the absolute path to the `uv` executable for `"command"`. Optional environment variables such as `EXPENSE_MCP_DB_PATH` and `EXPENSE_MCP_DEFAULT_CURRENCY` can be set in the server entry's `"env"` object.

## MCP capabilities

### Expenses and income

- `add_expense` and `add_income` record positive amounts with ISO `YYYY-MM-DD` dates, categories/source, currency, account and payment method.
- `get_transaction`, `update_expense`, `delete_expense`, and `delete_transaction` manage transaction records by ID.
- `search_expenses` supports date, category, subcategory, type, currency, account, payment method, note, amount, and pagination filters. Amount filters require a currency to avoid comparing unlike currencies.
- `list_expenses` retains the original inclusive date-range interface and supports pagination.
- `summarize`, `summarize_cashflow`, `monthly_report`, and `monthly_comparison` produce category and cash-flow reports without combining currencies.

Money is stored as integer minor units, so addition and report totals are exact. Existing `amount` and `total_amount` numeric fields are preserved for convenience; use `amount_decimal`, `total_amount_decimal`, and their `*_minor` fields where exact display or arithmetic matters. Currency precision follows common zero- and three-decimal ISO currency conventions; other three-letter codes use two decimal places.

Categories in `categories.json` are suggestions exposed by `expense://categories`, not a hard allow-list. This preserves existing customized and legacy category names.

### Budgets and recurring transactions

- `set_budget` upserts a monthly category or subcategory budget.
- `budget_vs_actual` reports budget, actual spend, remaining amount, and percent used.
- `delete_budget` removes a specific budget.
- `create_recurring_transaction` defines a daily, weekly, monthly, or yearly template. It does not create transactions automatically.
- `generate_due_transactions` records all due occurrences through a supplied date, exactly once, and advances each template's due date. `list_recurring_transactions` and `set_recurring_active` manage templates.

### Accounts, expense splits, and savings

- `create_account`, `list_accounts`, `account_balances`, and `set_account_active` track local accounts/payment sources and calculate balances from opening amounts and tagged transactions. Accounts are labels; no bank connection is made.
- `set_expense_splits`, `list_expense_splits`, and `settle_expense_split` track amounts owed by other people. Total shares cannot exceed the expense.
- `create_savings_goal`, `contribute_to_savings_goal`, `list_savings_goals`, and `set_savings_goal_active` track goal progress separately from expenses and cash-flow reports.
- Expense records can be marked reimbursable/reimbursed.

### Portability and backups

- `export_expenses` returns CSV text for a date range.
- `import_expenses` validates the complete CSV before writing it atomically. It skips exact matching transactions by default; callers can explicitly choose `add` or `error`.
- `backup_database`, `list_database_backups`, and `restore_database_backup` operate only on files in the configured backup directory. Restore verifies SQLite integrity and the required schema before replacing the active database.

### MCP prompts

- `monthly_spending_review` guides a grounded month-end review using the reporting tools.
- `unusual_expense_review` guides a date-bounded search for large or duplicate-looking entries and instructs the assistant to ask before changing data.

## Development and tests

Run the focused standard-library test suite with `uv run python -m unittest discover -s tests -v`. Tests use temporary SQLite files and do not alter the personal database.
