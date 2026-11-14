# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import binascii
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.l10n_pl_eb_client import EnableBankingError, make_jwt

CONTROL_PANEL_URL = "https://enablebanking.com/cp/applications"
SIGN_IN_URL = "https://enablebanking.com/sign-in/"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class EnableBankingConnect(models.TransientModel):
    """'Connect bank' wizard: guide to the Enable Banking application (once),
    the application ID and key (once per company), then the bank."""

    _name = "l10n_pl.eb.connect"
    _description = "Enable Banking: connect bank"

    step = fields.Selection(
        selection=[
            ("start", "Start"),
            ("guide", "Guide"),
            ("credentials", "Application"),
            ("bank", "Bank"),
        ],
        default="start",
        required=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Journal to connect",
        domain="[('type', '=', 'bank'), ('company_id', '=', company_id)]",
    )
    has_credentials = fields.Boolean(compute="_compute_has_credentials")
    environment = fields.Selection(
        selection=[
            ("production", "Production: my real accounts"),
            ("sandbox", "Sandbox: test banks with sample data"),
        ],
        default="production",
        required=True,
    )
    redirect_url = fields.Char(compute="_compute_redirect_url")
    control_panel_url = fields.Char(default=CONTROL_PANEL_URL, readonly=True)
    sign_in_url = fields.Char(default=SIGN_IN_URL, readonly=True)
    application_id = fields.Char(string="Application ID")
    key_file = fields.Binary(string="Key file (.pem)", attachment=False)
    key_filename = fields.Char()
    private_key = fields.Text(string="Private key (PEM)")
    paste_key = fields.Boolean(string="Paste the key instead of uploading the file")
    country_code = fields.Char(string="Country", default="PL")
    all_countries = fields.Boolean(
        string="Banks from all countries",
        help="List every bank available through Enable Banking, not only the "
        "banks of the selected country (e.g. Revolut or a bank abroad).",
    )
    psu_type = fields.Selection(
        selection=[("business", "Business"), ("personal", "Personal")],
        string="Account type",
        default="business",
        required=True,
    )
    bank_search = fields.Char(string="Search bank")
    load_error = fields.Char(readonly=True)
    active_connection_count = fields.Integer(compute="_compute_active_connection_count")
    line_ids = fields.One2many("l10n_pl.eb.connect.bank", "wizard_id")

    @api.depends("company_id")
    def _compute_has_credentials(self):
        for wizard in self:
            company = wizard.company_id.sudo()
            wizard.has_credentials = bool(
                company.l10n_pl_eb_application_id and company.l10n_pl_eb_private_key
            )

    @api.depends("company_id")
    def _compute_active_connection_count(self):
        for wizard in self:
            wizard.active_connection_count = self.env[
                "l10n_pl.eb.connection"
            ].search_count(
                [("company_id", "=", wizard.company_id.id), ("state", "=", "active")]
            )

    @api.constrains("company_id")
    def _check_company_allowed(self):
        for wizard in self:
            if wizard.company_id not in self.env.user.company_ids:
                raise AccessError(
                    self.env._("You cannot connect banks for this company.")
                )

    def _compute_redirect_url(self):
        for wizard in self:
            wizard.redirect_url = (
                wizard.get_base_url().rstrip("/") + "/l10n_pl_enablebanking/callback"
            )

    @api.onchange("key_file", "key_filename")
    def _onchange_key_filename(self):
        """The .pem file downloaded from Enable Banking is named after the
        application ID."""
        match = UUID_RE.search((self.key_filename or "").lower())
        if match and not self.application_id:
            self.application_id = match.group(0)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    @api.model
    def _open_new(self, journal=None):
        company = (journal and journal.company_id) or self.env.company
        wizard = self.create(
            {
                "company_id": company.id,
                "journal_id": journal.id if journal else False,
                "country_code": (company.country_id.code or "PL").upper(),
            }
        )
        if wizard.has_credentials:
            wizard.step = "bank"
            try:
                wizard._load_banks()
            except UserError as error:
                # Still open the wizard: the key can be changed from here.
                wizard.load_error = str(error)
        return wizard._reopen()

    def _reopen(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "name": self.env._("Connect bank"),
            "view_mode": "form",
            "target": "new",
            "context": dict(self.env.context, dialog_size="large"),
        }

    def action_show_guide(self):
        self.step = "guide"
        return self._reopen()

    def action_have_application(self):
        self.step = "credentials"
        return self._reopen()

    def action_back(self):
        previous = {"guide": "start", "credentials": "start", "bank": "credentials"}
        self.step = previous.get(self.step, "start")
        return self._reopen()

    # ------------------------------------------------------------------
    # Application ID and key
    # ------------------------------------------------------------------
    def _clean_key(self):
        key = (self.private_key or "").strip()
        if not key and self.key_file:
            try:
                key = base64.b64decode(self.key_file).decode().strip()
            except (binascii.Error, UnicodeDecodeError) as error:
                raise UserError(
                    self.env._("The key file is not a text .pem file.")
                ) from error
        return key

    def action_save_credentials(self):
        """Check the application ID and key against Enable Banking and store
        them on the company."""
        self.ensure_one()
        application_id = (self.application_id or "").strip()
        if not application_id and self.key_filename:
            match = UUID_RE.search(self.key_filename.lower())
            application_id = match.group(0) if match else ""
        key = self._clean_key()
        if not key:
            raise UserError(
                self.env._(
                    "Upload the .pem file downloaded when you registered the "
                    "application, or paste its content."
                )
            )
        if not application_id:
            raise UserError(
                self.env._(
                    "Enter the application ID: it is the name of the .pem file "
                    "without the extension, or the ID shown on the list of API "
                    "applications in the Enable Banking control panel."
                )
            )
        try:
            make_jwt(application_id, key)
        except EnableBankingError as error:
            raise UserError(
                self.env._(
                    "This is not a valid private key. Use the .pem file "
                    "downloaded from Enable Banking. (%s)",
                    error,
                )
            ) from error
        self._check_company_allowed()
        company = self.company_id.sudo()
        company.write(
            {
                "l10n_pl_eb_application_id": application_id,
                "l10n_pl_eb_private_key": key,
            }
        )
        try:
            self._get_aspsps()
        except EnableBankingError as error:
            # The UserError rolls the new key back.
            if not error.status:
                raise UserError(
                    self.env._(
                        "Odoo could not reach Enable Banking (%s). Check that "
                        "the server has internet access and try again.",
                        error,
                    )
                ) from error
            raise UserError(
                self.env._(
                    "Enable Banking did not accept this application ID and "
                    "key: %s. Check that the ID matches the name of the .pem "
                    "file and that the application is active.",
                    error,
                )
            ) from error
        self.write(
            {
                "application_id": False,
                "private_key": False,
                "key_file": False,
                "key_filename": False,
                "paste_key": False,
                "step": "bank",
            }
        )
        self._load_banks()
        return self._reopen()

    # ------------------------------------------------------------------
    # Bank
    # ------------------------------------------------------------------
    def _get_aspsps(self):
        """Isolated for mocking in tests."""
        country = None if self.all_countries else (self.country_code or "PL").upper()
        return self.company_id._l10n_pl_eb_client().get_aspsps(
            country, psu_type=self.psu_type
        )

    def _load_banks(self):
        self.ensure_one()
        try:
            aspsps = self._get_aspsps()
        except EnableBankingError as error:
            raise UserError(str(error)) from error
        self.load_error = False
        needle = (self.bank_search or "").lower()
        commands = [fields.Command.clear()]
        for aspsp in sorted(aspsps, key=lambda a: (a.get("name") or "").lower()):
            searched = "{} {}".format(
                aspsp.get("name") or "", aspsp.get("country") or ""
            )
            if needle and needle not in searched.lower():
                continue
            commands.append(
                fields.Command.create(
                    {
                        "name": aspsp.get("name"),
                        "country_code": aspsp.get("country"),
                        "bic": aspsp.get("bic"),
                        "max_validity": aspsp.get("maximum_consent_validity") or 0,
                        "beta": bool(aspsp.get("beta")),
                    }
                )
            )
        self.line_ids = commands

    def action_load_banks(self):
        self.ensure_one()
        self._load_banks()
        return self._reopen()

    def _connect(self, bank_line):
        self.ensure_one()
        connection = self.env["l10n_pl.eb.connection"].create(
            {
                "name": bank_line.name,
                "aspsp_name": bank_line.name,
                "aspsp_country": (
                    bank_line.country_code or self.country_code or "PL"
                ).upper(),
                "psu_type": self.psu_type,
                "max_validity": bank_line.max_validity,
                "company_id": self.company_id.id,
                "target_journal_id": self.journal_id.id,
            }
        )
        return connection._start_authorization()


class EnableBankingConnectBank(models.TransientModel):
    _name = "l10n_pl.eb.connect.bank"
    _description = "Enable Banking: bank (ASPSP)"
    _order = "name"

    wizard_id = fields.Many2one("l10n_pl.eb.connect", required=True, ondelete="cascade")
    name = fields.Char(string="Bank", required=True)
    country_code = fields.Char(string="Country")
    bic = fields.Char(string="BIC")
    max_validity = fields.Integer(string="Maximum consent validity (s)")
    beta = fields.Boolean()

    def action_select(self):
        self.ensure_one()
        return self.wizard_id._connect(self)
