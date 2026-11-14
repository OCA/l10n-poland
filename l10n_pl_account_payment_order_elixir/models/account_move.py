# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging
import re

from lxml import etree

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# Above this gross amount (PLN) split payment is mandatory for goods and services
# from annex 15 to the VAT act. Used only for a hint, never to tick the box.
MPP_THRESHOLD = 15000.0


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_pl_split_payment = fields.Boolean(
        string="Split payment (MPP)",
        copy=False,
        tracking=True,
        help="Pay this vendor bill with the split payment mechanism "
        "(mechanizm podzielonej płatności): the VAT part goes to the supplier's "
        "VAT account. Set automatically for bills imported from KSeF when the "
        "supplier marked the invoice with split payment (P_18A); otherwise tick "
        "it yourself.",
    )
    l10n_pl_split_payment_hint = fields.Boolean(
        string="Split payment may apply",
        compute="_compute_l10n_pl_split_payment_hint",
        help="PLN bill above 15 000 PLN gross with VAT from a Polish supplier, "
        "not marked as split payment: check whether it covers goods or services "
        "from annex 15 to the VAT act.",
    )

    @api.depends(
        "move_type",
        "currency_id",
        "amount_total",
        "amount_tax",
        "partner_id",
        "l10n_pl_split_payment",
    )
    def _compute_l10n_pl_split_payment_hint(self):
        for move in self:
            move.l10n_pl_split_payment_hint = bool(
                move.move_type == "in_invoice"
                and not move.l10n_pl_split_payment
                and move.currency_id.name == "PLN"
                and move.amount_tax > 0
                and move.amount_total > MPP_THRESHOLD
                and move._l10n_pl_partner_nip()
            )

    @api.depends("l10n_pl_split_payment_hint")
    def _compute_alerts(self):
        return super()._compute_alerts()

    def _get_alerts(self):
        alerts = super()._get_alerts()
        if self.l10n_pl_split_payment_hint and self.payment_state not in (
            "paid",
            "in_payment",
            "reversed",
        ):
            alerts["l10n_pl_split_payment_hint"] = {
                "level": "info",
                "message": self.env._(
                    "This bill is above 15 000 PLN gross. If it covers goods or "
                    "services from annex 15 to the VAT act, tick Split payment "
                    "(MPP) before you pay it."
                ),
            }
        return alerts

    def _l10n_pl_partner_nip(self):
        """Polish NIP (10 digits) of the commercial partner, or False."""
        self.ensure_one()
        vat = (self.commercial_partner_id.vat or "").upper().replace(" ", "")
        if vat.startswith("PL"):
            vat = vat[2:]
        digits = re.sub(r"\D", "", vat)
        if len(digits) == 10 and not re.search(r"[A-Z]", vat):
            return digits
        return False

    def _l10n_pl_split_payment_invoice_number(self):
        self.ensure_one()
        return self.ref or self.payment_reference or self.name

    def l10n_pl_edi_get_ksef_bill_vals_from_xml(self, xml_content):
        """Take split payment (P_18A) and the supplier bank account (NrRB) from
        the KSeF FA(3) invoice, like the e-invoice says."""
        vals = super().l10n_pl_edi_get_ksef_bill_vals_from_xml(xml_content)
        if not vals:
            return vals
        try:
            root = etree.fromstring(
                xml_content.encode() if isinstance(xml_content, str) else xml_content
            )
        except (etree.XMLSyntaxError, ValueError):
            return vals
        split = root.find("{*}Fa/{*}Adnotacje/{*}P_18A")
        vals["l10n_pl_split_payment"] = split is not None and (
            (split.text or "").strip() == "1"
        )
        bank = self._l10n_pl_ksef_partner_bank(root, vals.get("partner_id"))
        if bank:
            vals["partner_bank_id"] = bank.id
        return vals

    def _l10n_pl_ksef_partner_bank(self, root, partner_id):
        """Supplier bank account from NrRB. An account the supplier did not use
        before is created as not trusted (allow_out_payment off), so nobody pays
        to a new account without checking it first. A user who may not create
        bank accounts still gets the bill, only without the account."""
        if not partner_id:
            return self.env["res.partner.bank"]
        numbers = [
            re.sub(r"\s", "", node.text or "")
            for node in root.findall("{*}Fa/{*}Platnosc/{*}RachunekBankowy/{*}NrRB")
        ]
        numbers = [n for n in numbers if n]
        if not numbers:
            return self.env["res.partner.bank"]
        partner = self.env["res.partner"].browse(partner_id).commercial_partner_id
        banks = self.env["res.partner.bank"].search(
            [("partner_id", "child_of", partner.id)]
        )
        for number in numbers:
            for bank in banks:
                if bank.sanitized_acc_number in (number, "PL" + number):
                    return bank
        number = numbers[0]
        if re.fullmatch(r"\d{26}", number):
            number = "PL" + number
        try:
            return self.env["res.partner.bank"]._find_or_create_bank_account(
                account_number=number,
                partner=partner,
                company=self[:1].company_id or self.env.company,
            )
        except (AccessError, UserError) as error:
            _logger.info("KSeF bill: bank account %s not added: %s", number, error)
            return self.env["res.partner.bank"]
