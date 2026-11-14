# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import api, models


class AccountPaymentMethod(models.Model):
    _inherit = "account.payment.method"

    @api.model
    def _get_payment_method_information(self):
        res = super()._get_payment_method_information()
        res["pl_elixir"] = {"mode": "multi", "type": ("bank",)}
        return res
