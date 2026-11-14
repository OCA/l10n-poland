# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import fields, models
from odoo.exceptions import UserError

from . import elixir


class AccountPaymentOrder(models.Model):
    _inherit = "account.payment.order"

    def generate_payment_file(self):
        self.ensure_one()
        if self.payment_method_id.code != "pl_elixir":
            return super().generate_payment_file()
        try:
            records = [
                self._l10n_pl_elixir_record(payment) for payment in self.payment_ids
            ]
            content = elixir.build_file(records)
        except elixir.ElixirError as error:
            raise UserError(self._l10n_pl_elixir_error_message(error)) from error
        day = fields.Date.context_today(self).strftime("%Y%m%d")
        return content, f"ELIXIR_{self.name.replace('/', '-')}_{day}.txt"

    def _l10n_pl_elixir_error_message(self, error):
        value = error.value
        if error.code == "not_nrb":
            return self.env._(
                'Account number "%s" is not a Polish NRB (26 digits).', value
            )
        if error.code == "check_digits":
            return self.env._('Account number "%s" has wrong check digits.', value)
        if error.code == "amount":
            return self.env._("The transfer amount must be positive (%s).", value)
        if error.code == "nip":
            return self.env._(
                'Split payment needs the recipient NIP (10 digits), not "%s".', value
            )
        if error.code == "empty":
            return self.env._("The payment order has no transfers.")
        return str(error)

    @staticmethod
    def _l10n_pl_address_lines(partner, street2=True):
        streets = [partner.street, partner.street2] if street2 else [partner.street]
        return [
            partner.name or "",
            " ".join(filter(None, streets)),
            " ".join(filter(None, [partner.zip, partner.city])),
        ]

    def _l10n_pl_elixir_record(self, payment):
        if payment.currency_id.name != "PLN":
            raise elixir.ElixirError(
                self.env._("Elixir-O transfers are in PLN only (%s).", payment.name)
            )
        company_bank = self.journal_id.bank_account_id
        if not company_bank:
            raise elixir.ElixirError(
                self.env._("Missing bank account on journal %s.", self.journal_id.name)
            )
        if not payment.partner_bank_id:
            raise elixir.ElixirError(
                self.env._(
                    "Missing bank account of %s.", payment.partner_id.display_name
                )
            )
        lines = payment.payment_line_ids
        split_lines = lines.filtered("l10n_pl_split_payment")
        recipient = payment.partner_bank_id.partner_id or payment.partner_id
        values = {
            "execution_date": payment.date,
            "amount": payment.amount,
            "sender_account": company_bank.acc_number,
            "recipient_account": payment.partner_bank_id.acc_number,
            # As in the files accepted by the bank so far: name, street, zip and
            # city of the company, without the second street line.
            "sender_name_lines": self._l10n_pl_address_lines(
                self.company_id.partner_id, street2=False
            ),
            "recipient_name_lines": self._l10n_pl_address_lines(
                recipient.commercial_partner_id if not recipient.street else recipient
            ),
            "title": payment.payment_reference or payment.memo or payment.name,
        }
        if split_lines:
            if len(lines) != 1:
                raise elixir.ElixirError(
                    self.env._(
                        "A split payment transfer must pay exactly one invoice (%s).",
                        payment.name,
                    )
                )
            move = lines.move_line_id.move_id
            values.update(
                mpp=True,
                vat_amount=lines.l10n_pl_vat_amount,
                recipient_nip=(
                    move._l10n_pl_partner_nip()
                    if move
                    else lines.partner_id.commercial_partner_id.vat
                ),
                invoice_number=(
                    move._l10n_pl_split_payment_invoice_number()
                    if move
                    else lines.communication
                ),
            )
        return elixir.record(**values)
