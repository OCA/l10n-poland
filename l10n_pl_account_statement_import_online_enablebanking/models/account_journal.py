# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .l10n_pl_eb_connection import INITIAL_PULL_DAYS, SERVICE


class AccountJournal(models.Model):
    _inherit = "account.journal"

    def action_l10n_pl_eb_connect(self):
        """Open the 'Connect bank' wizard. Started from a bank journal, the
        first new account returned by the bank is linked to this journal."""
        journal = self[:1]
        if journal and journal.type != "bank":
            journal = self.browse()
        return self.env["l10n_pl.eb.connect"]._open_new(journal=journal)

    def action_l10n_pl_eb_pull(self):
        """Download new transactions of the bank journals now."""
        providers = self.online_bank_statement_provider_id.filtered(
            lambda p: p.service == SERVICE
        )
        now = fields.Datetime.now()
        for provider in providers:
            since = provider.last_successful_run or now - timedelta(
                days=INITIAL_PULL_DAYS
            )
            provider._pull(since, now)
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def _l10n_pl_eb_reconcile_action(self):
        """Bank lines waiting for reconciliation: the reconciliation screen of
        account_reconcile_oca when installed, a plain list otherwise."""
        oca_action = self.env.ref(
            "account_reconcile_oca.action_bank_statement_line_reconcile_all",
            raise_if_not_found=False,
        )
        if oca_action:
            return oca_action._get_action_dict()
        return self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_pl_account_statement_import_online_enablebanking."
            "l10n_pl_eb_statement_line_to_reconcile_action"
        )

    def action_l10n_pl_eb_add_bank(self):
        """Connect one more bank: every account it returns gets its own new
        journal (its own tile on the dashboard)."""
        company = self[:1].company_id or self.env.company
        return (
            self.env["l10n_pl.eb.connect"].with_company(company)._open_new(journal=None)
        )

    # ------------------------------------------------------------------
    # Split payment (MPP): transfers between the settlement and VAT account
    # ------------------------------------------------------------------
    l10n_pl_vat_account_number = fields.Char(
        string="VAT account",
        help="Number of the VAT account (split payment) linked to this bank "
        "account. Transfers to and from it are recognised in downloaded "
        "transactions.",
    )
    l10n_pl_vat_journal_id = fields.Many2one(
        "account.journal",
        string="VAT account journal",
        domain="[('type', '=', 'bank'), ('id', '!=', id)]",
        check_company=True,
        help="Optional bank journal of the VAT account. Each recognised "
        "transfer gets a mirror line there, so the balance of the VAT "
        "account is visible in Odoo.",
    )
    l10n_pl_vat_match_regex = fields.Char(
        string="Recognise VAT transfers by",
        help="Optional regular expression searched (case insensitive) in the "
        "raw data of each downloaded transaction, e.g. a bank transaction "
        "code used by your bank for transfers to the VAT account. Leave "
        "empty to recognise them only by the VAT account number.",
    )
    l10n_pl_vat_empty_lines = fields.Boolean(
        string="Recognise split payment in lines without description",
        help="Some banks (e.g. Bank Millennium) send split payment movements "
        "without title, counterparty, account or transaction code. With this "
        "option an empty credit and an empty debit of the same day whose "
        "amount is the VAT of the credit (23%, 8% or 5%) become a customer "
        "payment (gross) and the transfer of its VAT to the VAT account; an "
        "empty credit equal to the VAT of an outgoing split payment becomes a "
        "transfer from the VAT account. A payment to a counterparty without "
        "title next to the bank fee for an outgoing VAT transfer becomes an "
        "outgoing split payment. An empty credit equal to (or smaller than) a "
        "payment to the tax office or ZUS of the same day becomes a transfer "
        "from the VAT account. Other empty lines are left as they are.",
    )
    l10n_pl_vat_auto_reconcile = fields.Boolean(
        string="Reconcile VAT transfers automatically",
        help="Book recognised transfers (and their mirror lines) on the "
        "internal transfer account of the company right after download.",
    )

    @api.constrains("l10n_pl_vat_match_regex")
    def _check_l10n_pl_vat_match_regex(self):
        for journal in self.filtered("l10n_pl_vat_match_regex"):
            try:
                re.compile(journal.l10n_pl_vat_match_regex)
            except re.error as error:
                raise ValidationError(
                    self.env._("Invalid regular expression: %s", error)
                ) from error

    def _l10n_pl_vat_is_transfer(self, account_number, raw_data):
        """True when a transaction moves money to or from the VAT account:
        the other party is the VAT account, or the user's rule matches."""
        self.ensure_one()
        vat = self._l10n_pl_vat_number()
        if vat and account_number and account_number[-26:] == vat[-26:]:
            return True
        if self.l10n_pl_vat_match_regex and raw_data:
            rule = self.l10n_pl_vat_match_regex
            return bool(re.search(rule, raw_data, re.IGNORECASE))
        return False

    def _l10n_pl_vat_number(self):
        self.ensure_one()
        return "".join((self.l10n_pl_vat_account_number or "").split()).upper()

    def _l10n_pl_vat_lang(self):
        return self.with_context(
            lang=self.company_id.partner_id.lang or self.env.lang or "en_US"
        )

    def action_l10n_pl_vat_create_journal(self):
        """Create the bank journal of the VAT account (no online sync) and the
        reconciliation model for the transfers."""
        self.ensure_one()
        self.check_access("write")
        number = self._l10n_pl_vat_number()
        journal = self._l10n_pl_vat_lang()
        if not self.l10n_pl_vat_journal_id:
            vat_journal = journal.create(
                {
                    "name": journal.env._(
                        "VAT account %(number)s", number=number[-4:] or ""
                    ).strip(),
                    "type": "bank",
                    "company_id": self.company_id.id,
                    "currency_id": self.currency_id.id,
                    "bank_acc_number": number or False,
                    "bank_statements_source": "undefined",
                }
            )
            self.l10n_pl_vat_journal_id = vat_journal
        self._l10n_pl_vat_reconcile_model()
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.journal",
            "res_id": self.l10n_pl_vat_journal_id.id,
            "view_mode": "form",
        }

    def _l10n_pl_vat_reconcile_model(self):
        """Reconciliation model: internal transfer for lines marked (MPP)."""
        self.ensure_one()
        company = self.company_id
        account = company.transfer_account_id
        journals = self | self.l10n_pl_vat_journal_id
        Model = self.env["account.reconcile.model"].with_context(
            lang=company.partner_id.lang or self.env.lang
        )
        model = Model.search(
            [
                ("company_id", "=", company.id),
                ("match_label", "=", "contains"),
                ("match_label_param", "=", "(MPP)"),
            ],
            limit=1,
        )
        if model:
            model.match_journal_ids |= journals
            return model
        if not account:
            return Model
        return Model.create(
            {
                "name": Model.env._("Internal transfer: VAT account (MPP)"),
                "company_id": company.id,
                "trigger": "manual",
                "match_label": "contains",
                "match_label_param": "(MPP)",
                "match_journal_ids": [(6, 0, journals.ids)],
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": account.id,
                            "amount_type": "percentage",
                            "amount_string": "100",
                            "label": Model.env._("Internal transfer (MPP)"),
                        },
                    )
                ],
            }
        )

    def _l10n_pl_vat_process_transfers(self):
        """Create mirror lines in the VAT journal and, when enabled, book the
        transfers on the internal transfer account. Safe to run again."""
        Line = self.env["account.bank.statement.line"]
        for journal in self.filtered("l10n_pl_vat_journal_id"):
            journal = journal._l10n_pl_vat_lang()
            vat_journal = journal.l10n_pl_vat_journal_id
            lines = Line.search(
                [
                    ("journal_id", "=", journal.id),
                    ("transaction_type", "=", "MPP"),
                ]
            )
            for line in lines:
                mirror_id = f"mpp-{line.unique_import_id or line.id}"
                mirror = Line.search(
                    [
                        ("journal_id", "=", vat_journal.id),
                        ("unique_import_id", "=", mirror_id),
                    ],
                    limit=1,
                )
                if not mirror:
                    mirror = Line.create(
                        {
                            "journal_id": vat_journal.id,
                            "date": line.date,
                            "amount": -line.amount,
                            "payment_ref": journal.env._(
                                "Transfer from the settlement account (MPP)"
                            )
                            if line.amount < 0
                            else journal.env._(
                                "Return to the settlement account (MPP)"
                            ),
                            "ref": line.ref,
                            "transaction_type": "MPP",
                            "unique_import_id": mirror_id,
                            "narration": line.narration,
                        }
                    )
                if journal.l10n_pl_vat_auto_reconcile:
                    (line | mirror)._l10n_pl_vat_book_transfer()
