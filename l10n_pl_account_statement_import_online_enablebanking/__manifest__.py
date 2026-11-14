# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Poland - Online Bank Statements: Enable Banking",
    "summary": "Pull bank transactions into bank statements through Enable Banking "
    "(PSD2 account information)",
    "version": "19.0.1.0.0",
    "category": "Accounting",
    "website": "https://github.com/OCA/l10n-poland",
    "author": "Leszek Szczepkowski, Odoo Community Association (OCA)",
    "maintainers": ["leszekszczepkowski"],
    "license": "AGPL-3",
    "development_status": "Alpha",
    "installable": True,
    "external_dependencies": {"python": ["cryptography", "requests"]},
    "depends": [
        "account_statement_import_online",
        "account_statement_import_file",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/l10n_pl_eb_security.xml",
        "wizards/l10n_pl_eb_connect_views.xml",
        "views/l10n_pl_eb_connection_views.xml",
        "views/online_bank_statement_provider.xml",
        "views/account_journal_views.xml",
        "views/res_config_settings_views.xml",
    ],
}
