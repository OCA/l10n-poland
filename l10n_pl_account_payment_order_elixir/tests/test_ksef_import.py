# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import tools
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .test_elixir import make_nrb

NEW_NRB = make_nrb("000000000000000000000004")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestKsefImport(AccountTestInvoicingCommon):
    """Split payment and the supplier account come from the KSeF invoice."""

    @classmethod
    @AccountTestInvoicingCommon.setup_country("pl")
    def setUpClass(cls):
        super().setUpClass()
        with tools.file_open(
            "l10n_pl_edi/tests/import_xmls/fa3_standard_bill.xml", mode="rb"
        ) as fd:
            cls.xml = fd.read().decode()
        # With account_payment_order, adding bank accounts needs the Payments group.
        cls.env.user.group_ids += cls.env.ref(
            "account_payment_order.group_account_payment"
        )

    def _vals(self, split="1", nrb=NEW_NRB):
        xml = self.xml.replace("<P_18A>2</P_18A>", f"<P_18A>{split}</P_18A>")
        if nrb:
            xml = xml.replace(
                "</TerminPlatnosci>",
                "</TerminPlatnosci><RachunekBankowy>"
                f"<NrRB>{nrb}</NrRB></RachunekBankowy>",
            )
        return self.env["account.move"].l10n_pl_edi_get_ksef_bill_vals_from_xml(
            xml.encode()
        )

    def test_split_payment_from_ksef(self):
        bill = self.env["account.move"].create(self._vals("1"))
        self.assertTrue(bill.l10n_pl_split_payment)
        bill = self.env["account.move"].create(self._vals("2"))
        self.assertFalse(bill.l10n_pl_split_payment)

    def test_new_account_is_not_trusted(self):
        bill = self.env["account.move"].create(self._vals())
        bank = bill.partner_bank_id
        self.assertEqual(bank.sanitized_acc_number, "PL" + NEW_NRB)
        self.assertEqual(bank.partner_id, bill.commercial_partner_id)
        self.assertFalse(bank.allow_out_payment)
        # The same account on the next invoice is reused, not duplicated.
        again = self.env["account.move"].create(self._vals())
        self.assertEqual(again.partner_bank_id, bank)

    def test_no_account_in_ksef(self):
        bill = self.env["account.move"].create(self._vals(nrb=None))
        self.assertTrue(bill.l10n_pl_split_payment)

    def test_no_right_to_add_account(self):
        """Without the right to add bank accounts the bill still comes in."""
        self.env.user.group_ids -= self.env.ref(
            "account_payment_order.group_account_payment"
        )
        vals = self._vals()
        self.assertNotIn("partner_bank_id", vals)
        self.assertTrue(vals["l10n_pl_split_payment"])
