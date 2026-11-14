# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Poland - Elixir-O payment files (with split payment)",
    "summary": "Generate Elixir-O files for Polish banks from payment orders, "
    "including split payment (MPP, mechanizm podzielonej płatności)",
    "version": "19.0.1.0.0",
    "category": "Accounting/Localizations",
    "website": "https://github.com/OCA/l10n-poland",
    "author": "Leszek Szczepkowski, Odoo Community Association (OCA)",
    "maintainers": ["leszekszczepkowski"],
    "license": "AGPL-3",
    "development_status": "Alpha",
    "installable": True,
    "depends": ["account_payment_order", "l10n_pl_edi"],
    "data": [
        "data/account_payment_method.xml",
        "views/account_move_views.xml",
        "views/account_payment_line_views.xml",
    ],
}
