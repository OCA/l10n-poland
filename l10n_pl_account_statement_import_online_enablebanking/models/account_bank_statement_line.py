# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    def _l10n_pl_vat_book_transfer(self):
        """Move the open (suspense) part of the line to the internal transfer
        account of the company. Lines already reconciled are left alone."""
        for line in self:
            account = line.company_id.transfer_account_id
            if line.is_reconciled or not account:
                continue
            _liquidity, suspense, _other = line._seek_for_lines()
            if not suspense:
                continue
            suspense.with_context(
                skip_account_move_synchronization=True,
                skip_readonly_check=True,
            ).write({"account_id": account.id})
