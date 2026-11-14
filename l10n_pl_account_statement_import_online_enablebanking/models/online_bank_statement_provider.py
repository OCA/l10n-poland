# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import hashlib
import json
import logging
import re
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

INITIAL_PULL_DAYS = 30
MAX_DETAIL_CALLS = 100
VAT_RATES = (23, 8, 5)
VAT_IN_TITLE = re.compile(r"/VAT/(\d+(?:[.,]\d{1,2})?)/", re.IGNORECASE)
# bank fee booked next to an outgoing split payment (e.g. Bank Millennium)
OUTGOING_VAT_FEE = "OUTGOING VAT TRANSFER"
# sort codes of the accounts of tax offices (NBP) and of ZUS
PUBLIC_SORT_CODES = ("1010", "60000002")


class OnlineBankStatementProvider(models.Model):
    _inherit = "online.bank.statement.provider"

    l10n_pl_eb_connection_id = fields.Many2one(
        "l10n_pl.eb.connection",
        string="Bank connection",
        readonly=True,
        copy=False,
        ondelete="set null",
        index="btree_not_null",
    )
    l10n_pl_eb_aspsp_name = fields.Char(
        string="Bank", related="l10n_pl_eb_connection_id.name"
    )
    l10n_pl_eb_valid_until = fields.Datetime(
        string="Consent valid until", related="l10n_pl_eb_connection_id.valid_until"
    )
    l10n_pl_eb_connection_state = fields.Selection(
        string="Connection status", related="l10n_pl_eb_connection_id.state"
    )
    l10n_pl_eb_account_uid = fields.Char(
        string="Account UID", readonly=True, copy=False
    )
    l10n_pl_eb_account_data = fields.Text(
        string="Account data from the bank",
        readonly=True,
        copy=False,
        groups="account.group_account_manager",
    )
    l10n_pl_eb_balance = fields.Monetary(
        string="Balance in the bank", readonly=True, copy=False
    )
    l10n_pl_eb_balance_type = fields.Char(
        string="Balance type", readonly=True, copy=False
    )
    l10n_pl_eb_balance_date = fields.Datetime(
        string="Balance checked on", readonly=True, copy=False
    )
    l10n_pl_eb_balances_data = fields.Text(
        string="Balances from the bank", readonly=True, copy=False
    )
    l10n_pl_eb_date_type = fields.Selection(
        selection=[
            ("booking_date", "Booking date"),
            ("value_date", "Value date"),
            ("transaction_date", "Transaction date"),
        ],
        string="Date used for statement lines",
        default="booking_date",
    )

    @api.model
    def _get_available_services(self):
        return super()._get_available_services() + [
            ("l10n_pl_enablebanking", "Enable Banking (PL)"),
        ]

    @api.depends("l10n_pl_eb_connection_id.aspsp_name", "l10n_pl_eb_connection_id.name")
    def _compute_name(self):
        """Readable name instead of '<journal> l10n_pl_enablebanking', e.g.
        'Bank – Enable Banking (Bank Millennium)'."""
        eb = self.filtered(lambda p: p.service == "l10n_pl_enablebanking")
        res = super(OnlineBankStatementProvider, self - eb)._compute_name()
        for provider in eb:
            journal = provider.journal_id.name or ""
            connection = provider.l10n_pl_eb_connection_id
            bank = connection.name or connection.aspsp_name or ""
            name = f"{journal} – Enable Banking" if journal else "Enable Banking"
            if bank and bank.casefold() not in journal.casefold():
                name = f"{name} ({bank})"
            provider.name = name
        return res

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _l10n_pl_eb_client(self):
        self.ensure_one()
        if self.l10n_pl_eb_connection_id:
            return self.l10n_pl_eb_connection_id._client()
        return self.company_id._l10n_pl_eb_client()

    def _l10n_pl_eb_own_iban(self):
        self.ensure_one()
        return (self.journal_id.bank_account_id.sanitized_acc_number or "").upper()

    def action_l10n_pl_eb_connect(self):
        """Open the 'Connect bank' wizard for the journal of this provider."""
        self.ensure_one()
        return self.journal_id.action_l10n_pl_eb_connect()

    def _l10n_pl_eb_initial_pull(self, days=INITIAL_PULL_DAYS):
        """Fetch the last month right after the bank was connected. Errors are
        logged on the provider instead of interrupting the redirect."""
        now = fields.Datetime.now()
        Line = self.env["account.bank.statement.line"]
        for provider in self:
            since = provider.last_successful_run or now - timedelta(days=days)
            fresh = not Line.search_count(
                [("journal_id", "=", provider.journal_id.id)], limit=1
            )
            try:
                with self.env.cr.savepoint():
                    provider.with_context(scheduled=True)._pull(since, now)
                    if fresh:
                        provider._l10n_pl_eb_set_opening_balance()
            except Exception as error:  # noqa: BLE001
                _logger.warning("Enable Banking: first pull failed: %s", error)
                provider.message_post(
                    body=self.env._("First download failed: %s", error)
                )

    def _l10n_pl_eb_set_opening_balance(self):
        """After the first download into an empty journal, start the first
        statement from the balance the bank had before the downloaded period,
        so the journal shows the same balance as the bank."""
        self.ensure_one()
        if not self.l10n_pl_eb_balance_date:
            return
        journal = self.journal_id
        currency = journal.currency_id or journal.company_id.currency_id
        lines = self.env["account.bank.statement.line"].search(
            [("journal_id", "=", journal.id), ("state", "=", "posted")]
        )
        opening = currency.round(self.l10n_pl_eb_balance - sum(lines.mapped("amount")))
        statements = lines.statement_id.sorted(lambda s: s.first_line_index or "")
        if not statements or currency.is_zero(opening):
            return
        for statement in statements:
            statement.balance_start += opening

    # ------------------------------------------------------------------
    # Pulling transactions
    # ------------------------------------------------------------------
    def _obtain_statement_data(self, date_since, date_until):
        self.ensure_one()
        if self.service != "l10n_pl_enablebanking":
            return super()._obtain_statement_data(date_since, date_until)
        return self._l10n_pl_eb_obtain_statement_data(date_since, date_until)

    def _l10n_pl_eb_lang(self):
        return self.company_id.partner_id.lang or self.env.lang or "en_US"

    def _create_or_update_statement(
        self, data, statement_date_since, statement_date_until
    ):
        res = super()._create_or_update_statement(
            data, statement_date_since, statement_date_until
        )
        if self.service == "l10n_pl_enablebanking":
            self.journal_id._l10n_pl_vat_process_transfers()
        return res

    def _l10n_pl_eb_check_consent(self):
        """Return False when the account is not linked or the consent of its
        connection expired (the connection warns before that)."""
        self.ensure_one()
        if not self.l10n_pl_eb_account_uid or not self.l10n_pl_eb_connection_id:
            return False
        return self.l10n_pl_eb_connection_id._check_consent()

    def _l10n_pl_eb_request_transactions(self, date_since, date_until):
        """Isolated for mocking in tests."""
        return list(
            self._l10n_pl_eb_client().get_transactions(
                self.l10n_pl_eb_account_uid,
                date_since.strftime("%Y-%m-%d"),
                date_until.strftime("%Y-%m-%d"),
            )
        )

    def _l10n_pl_eb_request_transaction_details(self, transaction_id):
        """Isolated for mocking in tests."""
        return self._l10n_pl_eb_client().get_transaction_details(
            self.l10n_pl_eb_account_uid, transaction_id
        )

    def _l10n_pl_eb_request_balances(self):
        """Isolated for mocking in tests."""
        return self._l10n_pl_eb_client().get_balances(self.l10n_pl_eb_account_uid)

    def _l10n_pl_eb_request_account_details(self):
        """Isolated for mocking in tests."""
        return self._l10n_pl_eb_client().get_account_details(
            self.l10n_pl_eb_account_uid
        )

    @api.model
    def _l10n_pl_eb_view(self, tr):
        """Transaction as returned by the list, completed with what the
        details call added (kept apart under ``_details``)."""
        view = dict(tr.get("_details") or {})
        view.update({k: v for k, v in tr.items() if v and k != "_details"})
        return view

    def _l10n_pl_eb_add_details(self, transactions):
        """Ask the bank for the details of transactions that came without a
        title and without a counterparty. Errors only skip the details."""
        self.ensure_one()
        asked = 0
        for tr in transactions:
            if asked >= MAX_DETAIL_CALLS:
                break
            view = self._l10n_pl_eb_view(tr)
            debit = view.get("credit_debit_indicator") == "DBIT"
            side = "creditor" if debit else "debtor"
            if (
                not view.get("transaction_id")
                or self._l10n_pl_eb_title(view)
                or (view.get(side) or {}).get("name")
            ):
                continue
            asked += 1
            try:
                details = self._l10n_pl_eb_request_transaction_details(
                    view["transaction_id"]
                )
            except Exception as error:  # noqa: BLE001 - details are optional
                _logger.info("Enable Banking: no transaction details: %s", error)
                continue
            if isinstance(details, dict) and details:
                tr["_details"] = details
        return transactions

    def _l10n_pl_eb_update_balance(self):
        """Store the balances reported by the bank. Errors are only logged."""
        order = ["ITBD", "CLBD", "ITAV", "CLAV", "XPCD", "VALU"]
        for provider in self:
            try:
                balances = provider._l10n_pl_eb_request_balances()
            except Exception as error:  # noqa: BLE001 - balance is optional
                _logger.info("Enable Banking: no balances: %s", error)
                continue
            if not balances:
                continue
            main = min(
                balances,
                key=lambda b: (
                    order.index(b.get("balance_type"))
                    if b.get("balance_type") in order
                    else len(order)
                ),
            )
            amount = (main.get("balance_amount") or {}).get("amount")
            provider.write(
                {
                    "l10n_pl_eb_balance": float(amount or 0.0),
                    "l10n_pl_eb_balance_type": main.get("balance_type") or "",
                    "l10n_pl_eb_balance_date": fields.Datetime.now(),
                    "l10n_pl_eb_balances_data": json.dumps(
                        balances, ensure_ascii=False, indent=1
                    ),
                }
            )

    def _l10n_pl_eb_store_account_data(self, account=None):
        """Keep what the bank said about the account: the session entry and,
        when available, the account details call."""
        self.ensure_one()
        data = {"session": account} if account else {}
        if not account and self.sudo().l10n_pl_eb_account_data:
            data = json.loads(self.sudo().l10n_pl_eb_account_data)
        try:
            details = self._l10n_pl_eb_request_account_details()
        except Exception as error:  # noqa: BLE001 - details are optional
            _logger.info("Enable Banking: no account details: %s", error)
            details = None
        if details:
            data["details"] = details
        self.sudo().l10n_pl_eb_account_data = json.dumps(
            data, ensure_ascii=False, indent=1, sort_keys=True
        )

    def _l10n_pl_eb_obtain_statement_data(self, date_since, date_until):
        if not self._l10n_pl_eb_check_consent():
            # An error (not an empty result) keeps last_successful_run, so the
            # missing days are downloaded once the consent is renewed.
            raise UserError(
                self.env._(
                    "%s is not connected to the bank or the consent expired. "
                    "Renew it in Bank > Bank connections.",
                    self.journal_id.display_name,
                )
            )
        # The API cannot be asked for future dates.
        now = fields.Datetime.now()
        date_until = min(date_until, now)
        if date_since > date_until:
            return [], {}
        transactions = self._l10n_pl_eb_request_transactions(date_since, date_until)
        transactions = self._l10n_pl_eb_add_details(transactions)
        self._l10n_pl_eb_update_balance()
        # texts written into the lines follow the language of the company,
        # also when the scheduled action runs as a technical user
        provider = self.with_context(lang=self._l10n_pl_eb_lang())
        own_iban = provider._l10n_pl_eb_own_iban()
        lines = []
        for sequence, transaction in enumerate(transactions, start=1):
            line = provider._l10n_pl_eb_prepare_line(transaction, own_iban)
            if line:
                line["sequence"] = sequence
                lines.append(line)
        provider._l10n_pl_eb_split_payment_pairs(lines)
        return lines, {}

    def _l10n_pl_eb_split_payment_pairs(self, lines):
        """Recognise split payment movements among lines the bank sent
        without any description (e.g. Bank Millennium), when the journal
        asks for it. On one day:

        * an empty credit (gross amount) and an empty debit equal to its VAT
          (23/123, 8/108 or 5/105) are a customer payment and the transfer
          of its VAT to the VAT account;
        * a debit to a counterparty without any title, next to the bank fee
          for an outgoing VAT transfer or to an empty credit equal to its
          VAT, is an outgoing split payment;
        * an empty credit equal to the VAT of an outgoing split payment
          (``/VAT/<amount>/`` in its title or recognised above) is a transfer
          from the VAT account;
        * an empty credit equal to a payment to the tax office or ZUS that
          day, or smaller when the VAT account did not cover all of it, is a
          transfer from the VAT account used for that payment.

        Other empty lines keep their neutral description. Removes the
        technical ``_l10n_pl_empty`` and ``_l10n_pl_no_title`` keys from all
        lines."""
        no_title = [line for line in lines if line.pop("_l10n_pl_no_title", False)]
        empty = [line for line in lines if line.pop("_l10n_pl_empty", False)]
        if not self.journal_id.l10n_pl_vat_empty_lines or not empty:
            return lines
        state = {
            "credits": [line for line in empty if line["amount"] > 0],
            "empty": {id(line) for line in empty},
            "used": set(),
        }
        self._l10n_pl_mpp_receipts(state, [ln for ln in empty if ln["amount"] < 0])
        self._l10n_pl_mpp_outgoing(state, lines, no_title)
        self._l10n_pl_mpp_titled(state, lines)
        self._l10n_pl_mpp_public(state, lines)
        return lines

    @staticmethod
    def _l10n_pl_mpp_cents(value):
        return round(abs(value) * 100)

    @classmethod
    def _l10n_pl_mpp_vat_of(cls, amount):
        cents = cls._l10n_pl_mpp_cents(amount)
        return {round(cents * rate / (100 + rate)) for rate in VAT_RATES}

    @staticmethod
    def _l10n_pl_mpp_note(line, text):
        note = line.get("narration") or Markup()
        line["narration"] = (
            Markup("%s<br/>%s") % (note, text) if note else Markup("%s") % text
        )

    def _l10n_pl_mpp_free_credit(self, state, day, amounts):
        for credit in state["credits"]:
            if id(credit) in state["used"] or credit["date"] != day:
                continue
            cents = self._l10n_pl_mpp_cents(credit["amount"])
            if any(abs(cents - amount) <= 1 for amount in amounts):
                return credit
        return None

    def _l10n_pl_mpp_from_vat_account(self, state, credit, text):
        state["used"].add(id(credit))
        credit.update(
            payment_ref=self.env._("Transfer from the VAT account (MPP)"),
            transaction_type="MPP",
        )
        self._l10n_pl_mpp_note(credit, text)

    def _l10n_pl_mpp_receipts(self, state, debits):
        """Empty gross credit and the empty debit of its VAT."""
        for debit in debits:
            # the VAT is computed from the credit, not from the debit
            credit = next(
                (
                    c
                    for c in state["credits"]
                    if id(c) not in state["used"]
                    and c["date"] == debit["date"]
                    and any(
                        abs(self._l10n_pl_mpp_cents(debit["amount"]) - v) <= 1
                        for v in self._l10n_pl_mpp_vat_of(c["amount"])
                    )
                ),
                None,
            )
            if not credit:
                continue
            state["used"].add(id(credit))
            debit.update(
                payment_ref=self.env._("Transfer to the VAT account (MPP)"),
                transaction_type="MPP",
            )
            credit.update(
                payment_ref=self.env._("Split payment receipt (gross)"),
                transaction_type="MPP brutto",
            )
            pair = self.env._(
                "Split payment: gross %(gross).2f, VAT %(vat).2f moved to the "
                "VAT account the same day.",
                gross=abs(credit["amount"]),
                vat=abs(debit["amount"]),
            )
            self._l10n_pl_mpp_note(debit, pair)
            self._l10n_pl_mpp_note(credit, pair)

    def _l10n_pl_mpp_outgoing(self, state, lines, no_title):
        """Outgoing split payments the bank sent without a title."""
        fee_days = {
            line["date"]
            for line in lines
            if line["amount"] < 0
            and OUTGOING_VAT_FEE in (line.get("payment_ref") or "").upper()
        }
        for debit in no_title:
            if (
                debit["amount"] >= 0
                or id(debit) in state["empty"]
                or not debit.get("partner_name")
            ):
                continue
            credit = self._l10n_pl_mpp_free_credit(
                state, debit["date"], self._l10n_pl_mpp_vat_of(debit["amount"])
            )
            if not credit and debit["date"] not in fee_days:
                continue
            debit.update(
                payment_ref=self.env._("Outgoing VAT transfer (split payment)"),
                # not "MPP": it is no transfer to the VAT account
                transaction_type="MPP brutto",
            )
            self._l10n_pl_mpp_note(debit, debit["partner_name"])
            if credit:
                self._l10n_pl_mpp_from_vat_account(
                    state,
                    credit,
                    self.env._(
                        "VAT of the outgoing payment to %s", debit["partner_name"]
                    ),
                )

    def _l10n_pl_mpp_titled(self, state, lines):
        """Empty credit equal to ``/VAT/<amount>/`` of an outgoing payment."""
        for line in lines:
            match = VAT_IN_TITLE.search(line.get("payment_ref") or "")
            if line["amount"] >= 0 or not match:
                continue
            vat = round(float(match.group(1).replace(",", ".")) * 100)
            credit = self._l10n_pl_mpp_free_credit(state, line["date"], {vat})
            if credit:
                self._l10n_pl_mpp_from_vat_account(
                    state,
                    credit,
                    self.env._("VAT of the outgoing payment: %s", line["payment_ref"]),
                )

    def _l10n_pl_mpp_public(self, state, lines):
        """Empty credit used to pay the tax office or ZUS the same day."""
        public = [
            line
            for line in lines
            if line["amount"] < 0
            and id(line) not in state["empty"]
            and self._l10n_pl_eb_is_public_account(line.get("account_number"))
        ]
        done = set()
        for exact in (True, False):
            for debit in public:
                if id(debit) in done:
                    continue
                paid = self._l10n_pl_mpp_cents(debit["amount"])
                for credit in state["credits"]:
                    if id(credit) in state["used"] or credit["date"] != debit["date"]:
                        continue
                    diff = paid - self._l10n_pl_mpp_cents(credit["amount"])
                    if diff < 0 or (exact and diff) or (not exact and not diff):
                        continue
                    done.add(id(debit))
                    self._l10n_pl_mpp_from_vat_account(
                        state,
                        credit,
                        self.env._(
                            "Paid from the VAT account: %s", debit["payment_ref"]
                        ),
                    )
                    break

    @api.model
    def _l10n_pl_eb_is_public_account(self, number):
        """Polish account of a tax office (held at NBP) or of ZUS."""
        digits = re.sub(r"\s", "", number or "").upper()
        digits = digits.removeprefix("PL")
        return (
            len(digits) == 26
            and digits.isdigit()
            and digits[2:10].startswith(PUBLIC_SORT_CODES)
        )

    def _l10n_pl_eb_prepare_line(self, tr, own_iban):
        """Map one Enable Banking transaction to statement line values."""
        raw_data = json.dumps(tr, ensure_ascii=False, indent=1, sort_keys=True)
        unique_id = self._l10n_pl_eb_unique_id(
            {k: v for k, v in tr.items() if k != "_details"}
        )
        tr = self._l10n_pl_eb_view(tr)
        status = tr.get("status")
        if status and status != "BOOK":
            return False
        date_str = (
            tr.get(self.l10n_pl_eb_date_type or "booking_date")
            or tr.get("booking_date")
            or tr.get("value_date")
            or tr.get("transaction_date")
        )
        if not date_str:
            return False
        amount_data = tr.get("transaction_amount") or {}
        amount = float(amount_data.get("amount") or 0.0)
        is_debit = tr.get("credit_debit_indicator") == "DBIT"
        if is_debit:
            amount = -abs(amount)
        else:
            amount = abs(amount)
        line_date = fields.Date.to_date(date_str[:10])
        currency_code = amount_data.get("currency")
        journal_currency = self.journal_id.currency_id or self.company_id.currency_id
        if (
            currency_code
            and journal_currency
            and currency_code != journal_currency.name
        ):
            currency = self.env["res.currency"].search(
                [("name", "=", currency_code)], limit=1
            )
            if currency:
                amount = currency._convert(
                    amount, journal_currency, self.company_id, line_date
                )
        info = self._l10n_pl_eb_counterpart(tr, is_debit, own_iban)
        title = self._l10n_pl_eb_title(tr)
        text = self._l10n_pl_eb_text
        code = tr.get("bank_transaction_code") or {}
        code_text = text(code.get("description"))
        payment_ref = (
            title
            or info["name"]
            or text(tr.get("note"))
            or code_text
            or "{} {}".format(
                self.env._("Debit") if is_debit else self.env._("Credit"),
                fields.Date.to_string(line_date),
            )
        )
        reference = text(tr.get("reference_number") or tr.get("entry_reference"))
        transaction_type = " ".join(
            p
            for p in (text(code.get("code")), text(code.get("sub_code")), code_text)
            if p
        )
        empty = not (
            title
            or info["name"]
            or info["account"]
            or text(tr.get("note"))
            or transaction_type
        )
        if self.journal_id._l10n_pl_vat_is_transfer(info["account"], raw_data):
            # split payment: money moved between the settlement and VAT account
            payment_ref = (
                self.env._("Transfer to the VAT account (MPP)")
                if is_debit
                else self.env._("Transfer from the VAT account (MPP)")
            )
            transaction_type = "MPP"
        return {
            "date": line_date,
            "ref": reference or False,
            "payment_ref": payment_ref,
            "amount": amount,
            "unique_import_id": unique_id,
            "account_number": info["account"],
            "partner_name": info["name"] or False,
            "transaction_type": transaction_type,
            "narration": self._l10n_pl_eb_note(tr, info, title),
            "raw_data": raw_data,
            "_l10n_pl_empty": empty and transaction_type != "MPP",
            "_l10n_pl_no_title": not (title or text(tr.get("note"))),
        }

    @api.model
    def _l10n_pl_eb_text(self, value):
        """Flatten a string, a list or a dict of strings into one line."""
        if not value:
            return ""
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, list | tuple):
            return " ".join(p for p in (self._l10n_pl_eb_text(v) for v in value) if p)
        return " ".join(str(value).split())

    @api.model
    def _l10n_pl_eb_title(self, tr):
        """Payment title: unstructured remittance information first, then
        the structured one (e.g. a creditor reference)."""
        parts = []
        for key in (
            "remittance_information",
            "remittance_information_unstructured",
            "remittance_information_structured",
        ):
            text = self._l10n_pl_eb_text(tr.get(key))
            if text and text not in parts:
                parts.append(text)
        return " ".join(parts)

    @api.model
    def _l10n_pl_eb_counterpart(self, tr, is_debit, own_iban):
        """Name, address, account and bank of the other party."""
        text = self._l10n_pl_eb_text
        side = "creditor" if is_debit else "debtor"
        party = tr.get(side) or {}
        ultimate = tr.get(f"ultimate_{side}") or {}
        account = self._l10n_pl_eb_account_number(tr.get(f"{side}_account"))
        if not account:
            for extra in tr.get(f"{side}_account_additional_identification") or []:
                if isinstance(extra, dict) and extra.get("identification"):
                    account = str(extra["identification"]).replace(" ", "").upper()
                    break
        if account and own_iban and account.endswith(own_iban[-26:]):
            account = False
        agent = tr.get(f"{side}_agent") or {}
        address = party.get("postal_address") or {}
        return {
            "name": text(party.get("name") or ultimate.get("name")),
            "account": account or False,
            "bank": " ".join(
                p for p in (text(agent.get("name")), text(agent.get("bic_fi"))) if p
            ),
            "address": text(
                address.get("address_line")
                or [
                    address.get("street_name"),
                    address.get("building_number"),
                    address.get("post_code"),
                    address.get("town_name"),
                ]
            ),
        }

    @api.model
    def _l10n_pl_eb_account_number(self, account):
        if not account:
            return False
        number = account.get("iban") or (account.get("other") or {}).get(
            "identification"
        )
        return str(number).replace(" ", "").upper() if number else False

    def _l10n_pl_eb_unique_id(self, tr):
        """Stable id: the bank reference when given, otherwise a hash of the
        transaction. The base module prefixes it with the account and journal."""
        reference = tr.get("entry_reference") or tr.get("transaction_id")
        if reference:
            return str(reference)
        digest = hashlib.sha256(
            json.dumps(tr, sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        return f"h{digest}"

    @api.model
    def _l10n_pl_eb_note(self, tr, info=None, title=None):
        """Readable summary of what the bank sent, for the note of the
        statement line. The full JSON is kept in raw_data."""
        if info is None:
            info = self._l10n_pl_eb_counterpart(
                tr, tr.get("credit_debit_indicator") == "DBIT", ""
            )
        if title is None:
            title = self._l10n_pl_eb_title(tr)
        text = self._l10n_pl_eb_text
        code = tr.get("bank_transaction_code") or {}
        balance = tr.get("balance_after_transaction") or {}
        rows = [
            (self.env._("Title"), title),
            (self.env._("Counterparty"), info["name"]),
            (self.env._("Counterparty address"), info["address"]),
            (self.env._("Counterparty account"), info["account"]),
            (self.env._("Counterparty bank"), info["bank"]),
            (
                self.env._("Transaction code"),
                text([code.get("code"), code.get("sub_code"), code.get("description")]),
            ),
            (
                self.env._("Merchant category code"),
                text(tr.get("merchant_category_code")),
            ),
            (self.env._("Reference"), text(tr.get("reference_number"))),
            (self.env._("Bank reference"), text(tr.get("entry_reference"))),
            (self.env._("Booking date"), text(tr.get("booking_date"))),
            (self.env._("Value date"), text(tr.get("value_date"))),
            (self.env._("Transaction date"), text(tr.get("transaction_date"))),
            (
                self.env._("Balance after transaction"),
                text([balance.get("amount"), balance.get("currency")]),
            ),
            (self.env._("Bank note"), text(tr.get("note"))),
        ]
        return Markup("<br/>").join(
            Markup("%s: %s") % (label, value) for label, value in rows if value
        )

    # ------------------------------------------------------------------
    # Refresh descriptions of lines already imported
    # ------------------------------------------------------------------
    def _l10n_pl_eb_refresh_lines(self, date_since, date_until):
        """Download the period again and update the descriptions of the
        statement lines already imported (matched by their import id).
        Reconciled lines only get raw_data, so nothing posted changes.
        Returns (updated, reconciled_kept, not_found)."""
        updated = kept = missing = 0
        Line = self.env["account.bank.statement.line"]
        for provider in self:
            provider = provider.with_context(lang=provider._l10n_pl_eb_lang())
            if not provider._l10n_pl_eb_check_consent():
                raise UserError(
                    self.env._(
                        "%s is not connected to the bank or the consent expired. "
                        "Renew it in Bank > Bank connections.",
                        provider.journal_id.display_name,
                    )
                )
            own_iban = provider._l10n_pl_eb_own_iban()
            journal = provider.journal_id
            transactions = provider._l10n_pl_eb_add_details(
                provider._l10n_pl_eb_request_transactions(date_since, date_until)
            )
            provider._l10n_pl_eb_update_balance()
            provider._l10n_pl_eb_store_account_data()
            prepared = [
                vals
                for vals in (
                    provider._l10n_pl_eb_prepare_line(tr, own_iban)
                    for tr in transactions
                )
                if vals
            ]
            provider._l10n_pl_eb_split_payment_pairs(prepared)
            for vals in prepared:
                journal._statement_line_import_update_unique_import_id(
                    vals, provider.account_number
                )
                line = Line.search(
                    [
                        ("unique_import_id", "=", vals["unique_import_id"]),
                        ("journal_id", "=", journal.id),
                    ],
                    limit=1,
                )
                if not line:
                    missing += 1
                    continue
                if line.is_reconciled:
                    line.raw_data = vals["raw_data"]
                    kept += 1
                    continue
                line.write(
                    {
                        key: vals[key]
                        for key in (
                            "payment_ref",
                            "ref",
                            "partner_name",
                            "account_number",
                            "transaction_type",
                            "narration",
                            "raw_data",
                        )
                    }
                )
                updated += 1
            provider.journal_id._l10n_pl_vat_process_transfers()
        return updated, kept, missing
