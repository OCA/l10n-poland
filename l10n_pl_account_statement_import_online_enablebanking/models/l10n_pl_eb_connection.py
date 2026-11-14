# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging
import secrets
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

from .l10n_pl_eb_client import EnableBankingClient, EnableBankingError

_logger = logging.getLogger(__name__)

# Enable Banking caps consents at the bank's ``maximum_consent_validity``;
# for most banks this is 180 days.
DEFAULT_CONSENT_DAYS = 180
CONSENT_WARNING_DAYS = 7
INITIAL_PULL_DAYS = 30
SERVICE = "l10n_pl_enablebanking"


class EnableBankingConnection(models.Model):
    """One consent given in one bank. It can cover several bank accounts;
    every account gets its own bank journal and statement provider."""

    _name = "l10n_pl.eb.connection"
    _description = "Enable Banking: bank connection"
    _inherit = "mail.thread"
    _check_company_auto = True
    _order = "id desc"

    name = fields.Char(string="Bank", required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    aspsp_name = fields.Char(string="Bank (ASPSP)", readonly=True)
    aspsp_country = fields.Char(string="Bank country", readonly=True)
    psu_type = fields.Selection(
        selection=[("business", "Business"), ("personal", "Personal")],
        string="Account type",
        default="business",
        readonly=True,
    )
    max_validity = fields.Integer(string="Maximum consent validity (s)", readonly=True)
    state = fields.Selection(
        selection=[
            ("draft", "Waiting for the bank"),
            ("active", "Connected"),
            ("expired", "Consent expired"),
            ("revoked", "Disconnected"),
        ],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
    )
    auth_state = fields.Char(
        readonly=True, copy=False, groups="account.group_account_manager"
    )
    session_id = fields.Char(
        string="Session ID",
        readonly=True,
        copy=False,
        groups="account.group_account_manager",
    )
    valid_until = fields.Datetime(
        string="Consent valid until", readonly=True, copy=False, tracking=True
    )
    expiry_warned = fields.Boolean(
        string="Consent expiry warning sent", readonly=True, copy=False
    )
    target_journal_id = fields.Many2one(
        "account.journal",
        string="Journal to connect",
        readonly=True,
        help="Bank journal from which the connection was started. The first "
        "account returned by the bank that matches no other journal is linked "
        "to it instead of creating a new journal.",
    )
    provider_ids = fields.One2many(
        "online.bank.statement.provider",
        "l10n_pl_eb_connection_id",
        string="Linked accounts",
    )
    journal_count = fields.Integer(compute="_compute_journal_count")

    @api.depends("provider_ids")
    def _compute_journal_count(self):
        for connection in self:
            connection.journal_count = len(connection.sudo().provider_ids.journal_id)

    # ------------------------------------------------------------------
    # API client
    # ------------------------------------------------------------------
    def _client(self):
        self.ensure_one()
        return self.company_id._l10n_pl_eb_client()

    def _redirect_url(self):
        return self.get_base_url().rstrip("/") + "/l10n_pl_enablebanking/callback"

    # ------------------------------------------------------------------
    # Consent flow
    # ------------------------------------------------------------------
    def _start_authorization(self):
        """Ask Enable Banking for an authorization URL and return an action
        sending the user to their bank."""
        self.ensure_one()
        days = DEFAULT_CONSENT_DAYS
        if self.max_validity:
            days = min(days, self.max_validity // 86400) or 1
        valid_until = datetime.now(timezone.utc) + timedelta(days=days)
        state = secrets.token_urlsafe(24)
        try:
            data = self._client().start_authorization(
                aspsp_name=self.aspsp_name or self.name,
                country=self.aspsp_country,
                valid_until=valid_until.isoformat(timespec="seconds"),
                redirect_url=self._redirect_url(),
                state=state,
                psu_type=self.psu_type or "business",
            )
        except EnableBankingError as error:
            raise UserError(str(error)) from error
        self.sudo().auth_state = state
        return {"type": "ir.actions.act_url", "url": data["url"], "target": "self"}

    def action_reconnect(self):
        """Renew the consent in the same bank."""
        self.ensure_one()
        self.check_access("write")
        return self._start_authorization()

    def _finish_authorization(self, code):
        """Called from the redirect controller with the code from the bank:
        create the session, link every account to a journal and fetch the
        last month of transactions."""
        self.ensure_one()
        try:
            session = self._client().create_session(code)
        except EnableBankingError as error:
            self.sudo().auth_state = False
            self.message_post(
                body=self.env._("Linking failed: %s", error),
            )
            return False
        self.sudo().write(
            {
                "auth_state": False,
                "session_id": session.get("session_id"),
                "valid_until": self._parse_datetime(
                    (session.get("access") or {}).get("valid_until")
                ),
                "expiry_warned": False,
                "state": "active",
            }
        )
        providers = self.env["online.bank.statement.provider"]
        skipped = []
        for account in session.get("accounts") or []:
            label = account.get("name") or account.get("uid") or "?"
            try:
                # One faulty account must not lose the whole consent: the
                # code from the bank cannot be used twice.
                with self.env.cr.savepoint():
                    provider = self._link_account(account, exclude=providers.journal_id)
            except Exception as error:  # noqa: BLE001
                _logger.warning("Enable Banking: account %s not linked", label)
                self.sudo().message_post(
                    body=self.env._(
                        "Account %(account)s not linked: %(error)s",
                        account=label,
                        error=error,
                    )
                )
                continue
            if provider:
                providers |= provider
            else:
                skipped.append(label)
        body = self.env._(
            "Connected. Linked accounts: %(accounts)s. Consent valid until %(until)s.",
            accounts=", ".join(providers.journal_id.mapped("display_name")) or "-",
            until=self.valid_until or "?",
        )
        if skipped:
            body += " " + self.env._(
                "Skipped (no account number): %s.", ", ".join(skipped)
            )
        self.message_post(body=body)
        providers._l10n_pl_eb_initial_pull()
        return True

    @api.model
    def _account_number(self, account):
        account_id = account.get("account_id") or {}
        number = account_id.get("iban") or (account_id.get("other") or {}).get(
            "identification"
        )
        return (number or "").replace(" ", "").upper()

    def _find_journal(self, number, currency=None, exclude=None):
        """Bank journal of the company with this account number and currency
        (an account with several currencies gets one journal per currency)."""
        journals = self.env["account.journal"].search(
            [
                ("type", "=", "bank"),
                ("company_id", "=", self.company_id.id),
                ("bank_account_id", "!=", False),
            ]
        )
        if exclude:
            journals -= exclude
        company_currency = self.company_id.currency_id
        for journal in journals:
            own = (journal.bank_account_id.sanitized_acc_number or "").upper()
            if (
                own
                and (own == number or own[-26:] == number[-26:])
                and (journal.currency_id or company_currency)
                == (currency or company_currency)
            ):
                return journal
        return journals.browse()

    def _find_unused_journal(self, currency=None, exclude=None):
        """Bank journal of the company that was never used for a bank account:
        no account number, no online provider, no statement lines, same
        currency. A new database has one ("Bank"); connecting the first bank
        fills it instead of leaving an empty tile next to a new journal."""
        journals = self.env["account.journal"].search(
            [
                ("type", "=", "bank"),
                ("company_id", "=", self.company_id.id),
                ("bank_account_id", "=", False),
            ],
            order="sequence, id",
        )
        if exclude:
            journals -= exclude
        company_currency = self.company_id.currency_id
        StatementLine = self.env["account.bank.statement.line"].sudo()
        for journal in journals:
            if journal.online_bank_statement_provider_id.l10n_pl_eb_account_uid:
                continue
            if (journal.currency_id or company_currency) != (
                currency or company_currency
            ):
                continue
            if StatementLine.search_count([("journal_id", "=", journal.id)], limit=1):
                continue
            return journal
        return journals.browse()

    def _get_currency(self, code):
        if not code:
            return self.env["res.currency"]
        currency = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", code)], limit=1)
        )
        if currency and not currency.active:
            currency.sudo().active = True
        return currency

    def _link_account(self, account, exclude=None):
        """Find or create the bank journal of one account and attach its
        statement provider to this connection."""
        self.ensure_one()
        number = self._account_number(account)
        if not number:
            return False
        currency = self._get_currency(account.get("currency"))
        if currency == self.company_id.currency_id:
            currency = self.env["res.currency"]
        journal = self._find_journal(number, currency, exclude=exclude)
        target = self.target_journal_id
        if (
            not journal
            and target
            and not target.bank_account_id
            and not target.online_bank_statement_provider_id.l10n_pl_eb_account_uid
            and (target.currency_id or self.company_id.currency_id)
            == (currency or self.company_id.currency_id)
        ):
            journal = target
            self._set_journal_bank_account(journal, number, account)
        if not journal and not target:
            journal = self._find_unused_journal(currency, exclude=exclude)
            if journal:
                self._set_journal_bank_account(journal, number, account)
        if not journal:
            Journal = self.env["account.journal"]
            journal = Journal.create(
                {
                    "name": self._journal_name(account, number),
                    "type": "bank",
                    "code": Journal._get_next_journal_default_code(
                        "bank", self.company_id
                    ),
                    "company_id": self.company_id.id,
                    "currency_id": currency.id,
                }
            )
            self._set_journal_bank_account(journal, number, account)
        provider = journal.online_bank_statement_provider_id
        if journal.bank_statements_source != "online" or provider.service != SERVICE:
            journal.write(
                {
                    "bank_statements_source": "online",
                    "online_bank_statement_provider": SERVICE,
                }
            )
            provider = journal.online_bank_statement_provider_id
        provider.write(
            {
                "l10n_pl_eb_connection_id": self.id,
                "l10n_pl_eb_account_uid": account.get("uid"),
                "statement_creation_mode": "monthly",
                "active": True,
            }
        )
        provider._l10n_pl_eb_store_account_data(account)
        return provider

    def _journal_name(self, account, number):
        label = account.get("name") or account.get("product") or self.name
        return f"{label} {number[-4:]}"

    def _set_journal_bank_account(self, journal, number, account):
        """Attach the company's bank account to the journal. Odoo then marks
        the account as trusted for payments, which writes on the bank account
        record, so this one field is written as superuser as well."""
        journal.sudo().bank_account_id = self._get_partner_bank(number, account)

    def _get_partner_bank(self, number, account):
        """Bank account record of the company for an account returned by the
        bank the user has just authorised.

        Only users who may manage this connection get here, but they do not
        always hold the right to create bank accounts: account_payment_order,
        for example, gives it only to its own "Accounting / Payments" group.
        This single record, always of the company's own partner, is therefore
        read and written as superuser and returned in the user's environment.
        """
        self.check_access("write")
        partner = self.company_id.partner_id
        PartnerBank = self.env["res.partner.bank"].sudo()
        partner_bank = PartnerBank.with_context(active_test=False).search(
            [
                ("sanitized_acc_number", "=", number),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )
        if partner_bank:
            if not partner_bank.active:
                partner_bank.active = True
            return partner_bank.with_env(self.env)
        vals = {
            "acc_number": number,
            "partner_id": partner.id,
            "company_id": self.company_id.id,
        }
        bic = (account.get("account_servicer") or {}).get("bic_fi")
        if bic:
            bank = self.env["res.bank"].search([("bic", "=", bic)], limit=1)
            if bank:
                vals["bank_id"] = bank.id
        return PartnerBank.create(vals).with_env(self.env)

    @api.model
    def _parse_datetime(self, value):
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def action_disconnect(self):
        self.check_access("write")
        for connection in self:
            if connection.sudo().session_id:
                try:
                    connection._client().delete_session(connection.sudo().session_id)
                except (EnableBankingError, UserError) as error:
                    _logger.info("Enable Banking session not deleted: %s", error)
            connection.sudo().write(
                {"session_id": False, "auth_state": False, "state": "revoked"}
            )
            connection.sudo().provider_ids.active = False
            connection.message_post(body=self.env._("Consent revoked."))

    # ------------------------------------------------------------------
    # Consent checks used when pulling
    # ------------------------------------------------------------------
    def _check_consent(self):
        """Return False when the consent is missing or expired; warn once
        a few days before it expires."""
        self.ensure_one()
        if self.state not in ("active", "expired"):
            return False
        now = fields.Datetime.now()
        if self.valid_until and self.valid_until <= now:
            if self.state != "expired":
                # Stop the scheduled pulls until the consent is renewed, so
                # that the gap is downloaded after renewal.
                self.sudo().state = "expired"
                self.sudo().provider_ids.active = False
                self.sudo().message_post(
                    body=self.env._(
                        "The consent expired. Click 'Renew consent' to log in "
                        "to the bank again."
                    ),
                    partner_ids=self.create_uid.partner_id.ids,
                )
            return False
        if (
            self.valid_until
            and self.valid_until <= now + timedelta(days=CONSENT_WARNING_DAYS)
            and not self.expiry_warned
        ):
            self.sudo().expiry_warned = True
            self.sudo().message_post(
                body=self.env._(
                    "The bank consent expires on %s. Renew it with 'Renew consent'.",
                    self.valid_until,
                ),
                partner_ids=self.create_uid.partner_id.ids,
            )
        return True

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_open_journals(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Bank accounts"),
            "res_model": "account.journal",
            "view_mode": "list,form",
            "domain": [("id", "in", self.provider_ids.journal_id.ids)],
        }

    def action_pull_now(self):
        self.ensure_one()
        self.check_access("write")
        now = fields.Datetime.now()
        for provider in self.provider_ids:
            since = provider.last_successful_run or now - timedelta(
                days=INITIAL_PULL_DAYS
            )
            provider._pull(since, now)
        return True


class ResCompany(models.Model):
    _inherit = "res.company"

    l10n_pl_eb_application_id = fields.Char(
        string="Enable Banking application ID",
        groups="account.group_account_manager",
        help="Application ID (key ID) from the Enable Banking control panel.",
    )
    l10n_pl_eb_private_key = fields.Text(
        string="Enable Banking private key (PEM)",
        groups="account.group_account_manager",
        help="RSA private key downloaded when the application was registered "
        "in the Enable Banking control panel.",
    )

    def _l10n_pl_eb_client(self):
        self.ensure_one()
        company = self.sudo()
        if not company.l10n_pl_eb_application_id or not company.l10n_pl_eb_private_key:
            raise UserError(
                self.env._(
                    "Enter the Enable Banking application ID and private key "
                    "first (Bank > Connect bank)."
                )
            )
        return EnableBankingClient(
            company.l10n_pl_eb_application_id, company.l10n_pl_eb_private_key
        )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    l10n_pl_eb_application_id = fields.Char(
        related="company_id.l10n_pl_eb_application_id"
    )

    def action_l10n_pl_eb_set_application(self):
        """Open the 'Connect bank' wizard on the application step: the key is
        uploaded as a file and checked against Enable Banking there."""
        self.ensure_one()
        wizard = (
            self.env["l10n_pl.eb.connect"]
            .with_company(self.company_id)
            .create({"company_id": self.company_id.id, "step": "credentials"})
        )
        return wizard._reopen()
