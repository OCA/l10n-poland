# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountPaymentLine(models.Model):
    _inherit = "account.payment.line"

    l10n_pl_split_payment = fields.Boolean(
        string="Split payment (MPP)",
        compute="_compute_l10n_pl_split_payment",
        store=True,
        readonly=False,
    )
    l10n_pl_vat_amount = fields.Monetary(
        string="VAT amount (MPP)",
        currency_field="currency_id",
        compute="_compute_l10n_pl_split_payment",
        store=True,
        readonly=False,
        help="VAT part of this transfer sent to the supplier's VAT account. "
        "For a partial payment it is proportional to the paid amount.",
    )

    @api.depends("move_line_id", "amount_currency")
    def _compute_l10n_pl_split_payment(self):
        for line in self:
            move = line.move_line_id.move_id
            if not move or not move.is_invoice(include_receipts=True):
                line.l10n_pl_split_payment = line.l10n_pl_split_payment or False
                continue
            line.l10n_pl_split_payment = move.l10n_pl_split_payment
            vat = 0.0
            if move.amount_total:
                vat = move.amount_tax * min(
                    1.0, abs(line.amount_currency) / abs(move.amount_total)
                )
            line.l10n_pl_vat_amount = line.currency_id.round(vat) if vat else 0.0

    def payment_line_hashcode(self):
        """A split payment transfer must point to exactly one invoice."""
        hashcode = super().payment_line_hashcode()
        if self.l10n_pl_split_payment:
            hashcode += f"-mpp-{self.id}"
        return hashcode

    def draft2open_payment_line_check(self):
        res = super().draft2open_payment_line_check()
        if self.order_id.payment_method_id.code != "pl_elixir":
            return res
        if self.currency_id.name != "PLN":
            raise UserError(
                self.env._(
                    "Elixir-O transfers are in PLN only (payment line %s).", self.name
                )
            )
        if self.l10n_pl_split_payment:
            move = self.move_line_id.move_id
            nip = (
                move._l10n_pl_partner_nip()
                if move
                else self.partner_id.commercial_partner_id.vat
            )
            if not nip:
                raise UserError(
                    self.env._(
                        "Split payment needs the Polish NIP of %(partner)s "
                        "(payment line %(line)s).",
                        partner=self.partner_id.display_name,
                        line=self.name,
                    )
                )
            if self.l10n_pl_vat_amount <= 0 or (
                self.l10n_pl_vat_amount > self.amount_currency
            ):
                raise UserError(
                    self.env._(
                        "Split payment VAT amount of payment line %s must be "
                        "positive and not above the transfer amount.",
                        self.name,
                    )
                )
        return res
