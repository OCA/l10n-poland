# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import csv

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models import elixir


def make_nrb(bank_and_account):
    """Return a valid Polish NRB for 24 digits (sort code + account)."""
    check = 98 - int(bank_and_account + "252100") % 97
    return f"{check:02d}{bank_and_account}"


COMPANY_NRB = make_nrb("000000000000000000000001")
SUPPLIER_NRB = make_nrb("000000000000000000000002")
SUPPLIER2_NRB = make_nrb("000000000000000000000003")


@tagged("post_install", "-at_install")
class TestElixir(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids += cls.env.ref(
            "account_payment_order.group_account_payment"
        )
        cls.pln = cls.env.ref("base.PLN")
        cls.pln.active = True
        cls.company = cls.env.company
        cls.company.partner_id.write(
            {
                "name": "Firma Przykładowa sp. z o.o.",
                "street": "ul. Kwiatowa 1",
                "zip": "80-001",
                "city": "Gdańsk",
            }
        )
        cls.supplier = cls.env["res.partner"].create(
            {
                "name": "Hurtownia Przykładowa Łąka",
                "vat": "PL5260250995",
                "street": "ul. Ogrodowa 7",
                "zip": "00-950",
                "city": "Warszawa",
            }
        )
        cls.supplier_no_nip = cls.env["res.partner"].create({"name": "Bez NIP"})
        cls.supplier_bank = cls.env["res.partner.bank"].create(
            {
                "acc_number": SUPPLIER_NRB,
                "partner_id": cls.supplier.id,
                "allow_out_payment": True,
            }
        )
        cls.no_nip_bank = cls.env["res.partner.bank"].create(
            {
                "acc_number": SUPPLIER2_NRB,
                "partner_id": cls.supplier_no_nip.id,
                "allow_out_payment": True,
            }
        )
        company_bank = cls.env["res.partner.bank"].create(
            {
                "acc_number": COMPANY_NRB,
                "partner_id": cls.company.partner_id.id,
                "company_id": cls.company.id,
            }
        )
        cls.method = cls.env.ref(
            "l10n_pl_account_payment_order_elixir.payment_method_pl_elixir"
        )
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.bank_journal.write(
            {
                "bank_account_id": company_bank.id,
                "outbound_payment_method_line_ids": [
                    Command.create({"payment_method_id": cls.method.id})
                ],
            }
        )
        cls.mode = cls.env["account.payment.mode"].create(
            {
                "name": "Elixir-O",
                "company_id": cls.company.id,
                "payment_method_id": cls.method.id,
                "bank_account_link": "fixed",
                "fixed_journal_id": cls.bank_journal.id,
                "group_lines": True,
            }
        )
        cls.tax_23 = cls.env["account.tax"].create(
            {
                "name": "VAT 23%",
                "amount": 23,
                "type_tax_use": "purchase",
                "company_id": cls.company.id,
            }
        )

    @classmethod
    def _bill(cls, partner, bank, net, ref, taxes=None):
        bill = cls.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": partner.id,
                "partner_bank_id": bank.id,
                "currency_id": cls.pln.id,
                "invoice_date": fields.Date.today(),
                "ref": ref,
                "payment_mode_id": cls.mode.id,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Kwiaty",
                            "price_unit": net,
                            "quantity": 1,
                            "tax_ids": [Command.set(taxes.ids if taxes else [])],
                        }
                    )
                ],
            }
        )
        bill.action_post()
        return bill

    def _order(self, bills):
        for bill in bills:
            action = bill.create_account_payment_line()
        return self.env["account.payment.order"].browse(action["res_id"])

    def _generate(self, order):
        order.draft2open()
        order.open2generated()
        attachment = self.env["ir.attachment"].search(
            [("res_model", "=", order._name), ("res_id", "=", order.id)]
        )
        self.assertEqual(len(attachment), 1)
        self.assertRegex(attachment.name, r"^ELIXIR_.+_\d{8}\.txt$")
        return base64.b64decode(attachment.datas).decode("ascii")

    def test_split_payment_is_not_guessed(self):
        """Like the invoice says: the box is never ticked by the amount alone,
        bills above 15 000 PLN with VAT only get a hint."""
        small = self._bill(self.supplier, self.supplier_bank, 1000, "FV-1", self.tax_23)
        big = self._bill(self.supplier, self.supplier_bank, 20000, "FV-2", self.tax_23)
        no_tax = self._bill(self.supplier, self.supplier_bank, 20000, "FV-3")
        self.assertFalse(any((small + big + no_tax).mapped("l10n_pl_split_payment")))
        self.assertFalse(small.l10n_pl_split_payment_hint)
        self.assertTrue(big.l10n_pl_split_payment_hint)
        self.assertIn("l10n_pl_split_payment_hint", big._get_alerts())
        self.assertFalse(no_tax.l10n_pl_split_payment_hint)
        big.l10n_pl_split_payment = True
        self.assertFalse(big.l10n_pl_split_payment_hint)
        self.assertNotIn("l10n_pl_split_payment_hint", big._get_alerts())

    def test_elixir_file(self):
        bill_a = self._bill(self.supplier, self.supplier_bank, 100, "FV 1/11")
        bill_b = self._bill(self.supplier, self.supplier_bank, 50, "FV 2/11")
        bill_mpp = self._bill(
            self.supplier, self.supplier_bank, 20000, "FV 3/11", self.tax_23
        )
        bill_mpp.l10n_pl_split_payment = True
        order = self._order(bill_a + bill_b + bill_mpp)
        mpp_line = order.payment_line_ids.filtered("l10n_pl_split_payment")
        self.assertEqual(len(mpp_line), 1)
        self.assertAlmostEqual(mpp_line.l10n_pl_vat_amount, 4600.0)
        content = self._generate(order)
        records = content.split("\r\n")
        self.assertEqual(records[-1], "")
        records = sorted(records[:-1])
        # Two normal bills grouped into one transfer + one split payment.
        self.assertEqual(len(records), 2)
        normal = [r for r in records if r.endswith('"51"')]
        split = [r for r in records if r.endswith('"53"')]
        self.assertEqual(len(normal), 1)
        self.assertEqual(len(split), 1)
        self.assertIn(",15000,", normal[0])
        self.assertIn(f'"{COMPANY_NRB}","{SUPPLIER_NRB}"', normal[0])
        self.assertIn(
            "Firma Przykladowa sp. z o.o.|ul. Kwiatowa 1|80-001 Gdansk", normal[0]
        )
        self.assertIn(",2460000,", split[0])
        self.assertIn(
            "/VAT/4600,00/IDC/5260250995/INV/FV 3/11",
            next(csv.reader([split[0]]))[11].replace("|", ""),
        )
        self.assertEqual(order.state, "generated")

    def test_partial_split_payment(self):
        bill = self._bill(
            self.supplier, self.supplier_bank, 20000, "FV 9/11", self.tax_23
        )
        bill.l10n_pl_split_payment = True
        order = self._order(bill)
        line = order.payment_line_ids
        line.amount_currency = 12300.0
        self.assertAlmostEqual(line.l10n_pl_vat_amount, 2300.0)

    def test_split_payment_requires_nip(self):
        bill = self._bill(
            self.supplier_no_nip, self.no_nip_bank, 100, "X1", self.tax_23
        )
        bill.l10n_pl_split_payment = True
        order = self._order(bill)
        self.assertTrue(order.payment_line_ids.l10n_pl_split_payment)
        with self.assertRaises(UserError):
            order.draft2open()

    def test_elixir_helpers(self):
        self.assertEqual(elixir.nrb("PL " + COMPANY_NRB), COMPANY_NRB)
        with self.assertRaises(elixir.ElixirError):
            elixir.nrb("PL00" + COMPANY_NRB[2:])
        self.assertEqual(elixir.grosze(12.345), 1235)
        self.assertEqual(elixir.fold('Łódź, "Żółw"'), "Lodz 'Zolw'")

    def test_same_record_as_production_format(self):
        """Golden record: the Elixir-O line this format produced for imports that
        a Polish bank accepted (split payment, Polish letters folded, 35-char
        parts)."""
        record = elixir.record(
            "20261114",
            12561.92,
            COMPANY_NRB,
            SUPPLIER_NRB,
            ("Firma Przykładowa sp. z o.o.", "ul. Kwiatowa 1", "80-001 Gdańsk"),
            ("Hurtownia Przykładowa Łąka", "ul. Ogrodowa 7", "00-950 Warszawa"),
            "FV 3/11",
            mpp=True,
            vat_amount=2349.05,
            recipient_nip="526-025-09-95",
            invoice_number="FV 3/11",
        )
        self.assertEqual(
            record,
            f'110,20261114,1256192,00000000,0,"{COMPANY_NRB}","{SUPPLIER_NRB}",'
            '"Firma Przykladowa sp. z o.o.|ul. Kwiatowa 1|80-001 Gdansk",'
            '"Hurtownia Przykladowa Laka|ul. Ogrodowa 7|00-950 Warszawa",'
            '0,00000000,"/VAT/2349,05/IDC/5260250995/INV/FV |3/11","","","53"',
        )

    def test_wrong_account_message(self):
        """An account that is not a Polish NRB stops the file with a readable
        message."""
        foreign = self.env["res.partner.bank"].create(
            {
                "acc_number": "12345678",
                "partner_id": self.supplier.id,
                "allow_out_payment": True,
            }
        )
        bill = self._bill(self.supplier, foreign, 100, "FV 5/11")
        order = self._order(bill)
        order.draft2open()
        with self.assertRaisesRegex(UserError, "is not a Polish NRB"):
            order.open2generated()

    def test_error_messages(self):
        """Every Elixir-O data error is shown as a readable message."""
        order = self.env["account.payment.order"]
        for code, value, text in (
            ("not_nrb", "123", "not a Polish NRB"),
            ("check_digits", "PL00", "wrong check digits"),
            ("amount", 0, "must be positive"),
            ("nip", "", "recipient NIP"),
            ("empty", "", "no transfers"),
        ):
            error = elixir.ElixirError("raw", code, value)
            self.assertIn(text, order._l10n_pl_elixir_error_message(error))
        self.assertEqual(
            order._l10n_pl_elixir_error_message(elixir.ElixirError("raw")), "raw"
        )
        with self.assertRaises(elixir.ElixirError):
            elixir.grosze(0)
        with self.assertRaises(elixir.ElixirError):
            elixir.mpp_title(10, "123", "FV 1")
        with self.assertRaises(elixir.ElixirError):
            elixir.build_file([])
        self.assertTrue(
            elixir.mpp_title(10, "5260250995", "FV 1", "x").endswith("/TXT/x")
        )

    def test_missing_supplier_account(self):
        bill = self._bill(self.supplier, self.supplier_bank, 100, "FV 6/11")
        order = self._order(bill)
        order.draft2open()
        order.payment_ids.partner_bank_id = False
        with self.assertRaisesRegex(UserError, "Missing bank account"):
            order.open2generated()
