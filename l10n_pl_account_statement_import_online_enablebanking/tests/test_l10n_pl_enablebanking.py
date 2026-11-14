# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import base64
import json
from datetime import timedelta
from unittest import mock

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, common, tagged
from odoo.tools import mute_logger

from ..models.l10n_pl_eb_client import (
    EnableBankingClient,
    EnableBankingError,
    make_jwt,
)

_online_provider_logger = (
    "odoo.addons.account_statement_import_online.models.online_bank_statement_provider"
)

_module_ns = "odoo.addons.l10n_pl_account_statement_import_online_enablebanking"
_provider_class = (
    _module_ns + ".models.online_bank_statement_provider.OnlineBankStatementProvider"
)
_client_class = _module_ns + ".models.l10n_pl_eb_client.EnableBankingClient"
_wizard_class = _module_ns + ".wizards.l10n_pl_eb_connect.EnableBankingConnect"

OWN_IBAN = "PL74000000000000000000000001"
EUR_IBAN = "PL47000000000000000000000002"
APP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ASPSPS = [
    {
        "name": "mBank",
        "country": "PL",
        "bic": "BREXPLPW",
        "maximum_consent_validity": 15552000,
    },
    {
        "name": "Bank Millennium",
        "country": "PL",
        "bic": "BIGBPLPW",
        "maximum_consent_validity": 7776000,
    },
]


def _b64decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _make_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def _patch_optional_calls(cls):
    """Details, balances and account details are extra calls to the bank;
    tests that do not look at them get empty answers."""
    cls.details_mock = cls.startClassPatcher(
        mock.patch(
            _provider_class + "._l10n_pl_eb_request_transaction_details",
            return_value={},
        )
    )
    cls.balances_mock = cls.startClassPatcher(
        mock.patch(_provider_class + "._l10n_pl_eb_request_balances", return_value=[])
    )
    cls.account_details_mock = cls.startClassPatcher(
        mock.patch(
            _provider_class + "._l10n_pl_eb_request_account_details",
            return_value={},
        )
    )


class EnableBankingCommon(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _patch_optional_calls(cls)
        cls.key, cls.pem = _make_pem()
        cls.company = cls.env.company
        cls.company.write(
            {"l10n_pl_eb_application_id": APP_ID, "l10n_pl_eb_private_key": cls.pem}
        )
        cls.currency_pln = cls.env.ref("base.PLN")
        cls.currency_pln.active = True
        cls.env.ref("base.EUR").active = True
        today = fields.Date.today()
        cls.day1 = fields.Date.to_string(today - timedelta(days=6))
        cls.day2 = fields.Date.to_string(today - timedelta(days=4))
        cls.transactions = [
            {
                "entry_reference": "E1",
                "transaction_amount": {"amount": "1230.00", "currency": "PLN"},
                "credit_debit_indicator": "CRDT",
                "status": "BOOK",
                "booking_date": cls.day1,
                "value_date": cls.day1,
                "debtor": {"name": "Klient Przykładowy sp. z o.o."},
                "debtor_account": {"iban": "PL20000000000000000000000003"},
                "remittance_information": ["FV 12/10/2026"],
            },
            {
                "transaction_id": "T2",
                "transaction_amount": {"amount": "246.00", "currency": "PLN"},
                "credit_debit_indicator": "DBIT",
                "status": "BOOK",
                "booking_date": cls.day2,
                "creditor": {"name": "Hurtownia Kwiatow"},
                "creditor_account": {"iban": "PL90000000000000000000000004"},
                "remittance_information": [
                    "/VAT/46,00/IDC/5260250995/INV/FZ 7/10",
                    "/TXT/kwiaty",
                ],
            },
            {
                "entry_reference": "P3",
                "transaction_amount": {"amount": "10.00", "currency": "PLN"},
                "credit_debit_indicator": "DBIT",
                "status": "PDNG",
                "booking_date": cls.day2,
            },
        ]

    def setUp(self):
        super().setUp()
        for patched, value in (
            (self.details_mock, {}),
            (self.balances_mock, []),
            (self.account_details_mock, {}),
        ):
            patched.reset_mock(return_value=True, side_effect=True)
            patched.return_value = value

    @classmethod
    def _session(cls, accounts=None):
        return {
            "session_id": "sess-1",
            "accounts": accounts
            if accounts is not None
            else [
                {
                    "uid": "uid-pln",
                    "account_id": {"iban": OWN_IBAN},
                    "name": "Konto firmowe",
                    "currency": "PLN",
                    "account_servicer": {"bic_fi": "BREXPLPW"},
                },
                {
                    "uid": "uid-eur",
                    "account_id": {"iban": EUR_IBAN},
                    "name": "Rachunek walutowy",
                    "currency": "EUR",
                },
            ],
            "access": {"valid_until": "2027-05-10T10:00:00+00:00"},
        }

    def _connect(self, journal=None, accounts=None, transactions=None):
        """Run the whole flow: wizard, bank, redirect back with a code."""
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            action = (
                self.env["account.journal"]
                .browse(journal.ids if journal else [])
                .action_l10n_pl_eb_connect()
            )
        wizard = self.env["l10n_pl.eb.connect"].browse(action["res_id"])
        line = wizard.line_ids.filtered(lambda line: line.name == "mBank")
        with mock.patch(
            _client_class + ".start_authorization",
            return_value={"url": "https://bank.example/auth"},
        ) as start:
            redirect = line.action_select()
        self.assertEqual(redirect["url"], "https://bank.example/auth")
        self.assertTrue(
            start.call_args.kwargs["redirect_url"].endswith(
                "/l10n_pl_enablebanking/callback"
            )
        )
        connection = self.env["l10n_pl.eb.connection"].search([], limit=1)
        self.assertEqual(connection.state, "draft")
        self.assertTrue(connection.auth_state)
        with (
            mock.patch(
                _client_class + ".create_session",
                return_value=self._session(accounts),
            ),
            mock.patch(
                _provider_class + "._l10n_pl_eb_request_transactions",
                return_value=transactions if transactions is not None else [],
            ),
        ):
            self.assertTrue(connection._finish_authorization("code"))
        return connection

    def _pull(self, provider, since, until):
        journal = provider.journal_id
        wizard = (
            self.env["online.bank.statement.pull.wizard"]
            .with_context(active_model="account.journal", active_id=journal.id)
            .create({"date_since": since, "date_until": until})
        )
        with mock.patch(
            _provider_class + "._l10n_pl_eb_request_transactions",
            return_value=self.transactions,
        ):
            wizard.action_pull()
        return self.env["account.bank.statement.line"].search(
            [("journal_id", "=", journal.id)], order="date"
        )

    def _pln_provider(self, connection):
        return connection.provider_ids.filtered(
            lambda p: p.l10n_pl_eb_account_uid == "uid-pln"
        )


@tagged("post_install", "-at_install")
class TestEnableBankingClient(EnableBankingCommon):
    def test_jwt(self):
        token = make_jwt("app-123", self.pem, now=1_700_000_000)
        header, payload, signature = token.split(".")
        self.assertEqual(
            json.loads(_b64decode(header)),
            {"typ": "JWT", "alg": "RS256", "kid": "app-123"},
        )
        claims = json.loads(_b64decode(payload))
        self.assertEqual(claims["iss"], "enablebanking.com")
        self.assertEqual(claims["aud"], "api.enablebanking.com")
        self.assertLessEqual(claims["exp"] - claims["iat"], 86400)
        # Raises InvalidSignature when wrong.
        self.key.public_key().verify(
            _b64decode(signature),
            f"{header}.{payload}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_client_paging(self):
        pages = [
            {"transactions": [{"entry_reference": "a"}], "continuation_key": "k1"},
            {"transactions": [{"entry_reference": "b"}], "continuation_key": None},
        ]
        client = EnableBankingClient("app-123", self.pem)
        with mock.patch.object(
            EnableBankingClient, "_request", side_effect=pages
        ) as request:
            result = list(client.get_transactions("uid", "2026-10-01", "2026-10-31"))
        self.assertEqual([t["entry_reference"] for t in result], ["a", "b"])
        self.assertEqual(
            request.call_args_list[1].kwargs["params"]["continuation_key"], "k1"
        )

    def test_network_error(self):
        client = EnableBankingClient("app-123", self.pem)
        with (
            mock.patch(
                "requests.request",
                side_effect=requests.ConnectionError("proxy refused"),
            ),
            self.assertRaises(EnableBankingError),
        ):
            client.get_aspsps("PL")

    def test_service_listed(self):
        self.assertIn(
            ("l10n_pl_enablebanking", "Enable Banking (PL)"),
            self.env["online.bank.statement.provider"]._get_available_services(),
        )


@tagged("post_install", "-at_install")
class TestEnableBankingWizard(EnableBankingCommon):
    def test_guide_without_application(self):
        self.company.write(
            {"l10n_pl_eb_application_id": False, "l10n_pl_eb_private_key": False}
        )
        action = self.env["account.journal"].action_l10n_pl_eb_add_bank()
        wizard = self.env["l10n_pl.eb.connect"].browse(action["res_id"])
        self.assertEqual(wizard.step, "start")
        self.assertFalse(wizard.has_credentials)
        wizard.action_show_guide()
        self.assertEqual(wizard.step, "guide")
        self.assertTrue(wizard.redirect_url.endswith("/l10n_pl_enablebanking/callback"))
        wizard.action_back()
        self.assertEqual(wizard.step, "start")
        wizard.action_have_application()
        self.assertEqual(wizard.step, "credentials")

    def test_with_application_goes_to_banks(self):
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            action = self.env["account.journal"].action_l10n_pl_eb_add_bank()
        wizard = self.env["l10n_pl.eb.connect"].browse(action["res_id"])
        self.assertEqual(wizard.step, "bank")
        self.assertEqual(wizard.line_ids.mapped("name"), ["Bank Millennium", "mBank"])
        wizard.bank_search = "mban"
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            wizard.action_load_banks()
        self.assertEqual(wizard.line_ids.mapped("name"), ["mBank"])

    def test_banks_from_all_countries(self):
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            action = self.env["account.journal"].action_l10n_pl_eb_add_bank()
        wizard = self.env["l10n_pl.eb.connect"].browse(action["res_id"])
        world = ASPSPS + [{"name": "Revolut", "country": "LT", "bic": "REVOLT21"}]
        with mock.patch(
            _client_class + "._request", return_value={"aspsps": world}
        ) as request:
            wizard.all_countries = True
            wizard.action_load_banks()
        self.assertNotIn("country", request.call_args.kwargs["params"])
        line = wizard.line_ids.filtered(lambda line: line.name == "Revolut")
        self.assertEqual(line.country_code, "LT")
        with mock.patch(
            _client_class + "._request", return_value={"aspsps": ASPSPS}
        ) as request:
            wizard.all_countries = False
            wizard.action_load_banks()
        self.assertEqual(
            request.call_args.kwargs["params"]["country"], wizard.country_code
        )

    def test_save_application_from_file(self):
        self.company.write(
            {"l10n_pl_eb_application_id": False, "l10n_pl_eb_private_key": False}
        )
        _key, pem = _make_pem()
        wizard = self.env["l10n_pl.eb.connect"].create({"step": "credentials"})
        wizard.key_file = base64.b64encode(pem.encode())
        wizard.key_filename = APP_ID + ".pem"
        wizard._onchange_key_filename()
        self.assertEqual(wizard.application_id, APP_ID)
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            wizard.action_save_credentials()
        self.assertEqual(self.company.l10n_pl_eb_application_id, APP_ID)
        self.assertEqual(self.company.l10n_pl_eb_private_key, pem.strip())
        self.assertEqual(wizard.step, "bank")
        self.assertEqual(len(wizard.line_ids), 2)
        self.assertFalse(wizard.key_file)
        self.assertFalse(wizard.private_key)

    def test_transient_vacuum(self):
        """No wizard field may shadow an ORM method (e.g. ``search``)."""
        with mock.patch(_wizard_class + "._get_aspsps", return_value=ASPSPS):
            self.env["account.journal"].action_l10n_pl_eb_add_bank()
        for model in ("l10n_pl.eb.connect", "l10n_pl.eb.connect.bank"):
            self.env[model]._transient_vacuum()

    def test_missing_application_id(self):
        _key, pem = _make_pem()
        wizard = self.env["l10n_pl.eb.connect"].create(
            {
                "step": "credentials",
                "key_file": base64.b64encode(pem.encode()),
                "key_filename": "klucz-enable-banking.pem",
            }
        )
        wizard._onchange_key_filename()
        self.assertFalse(wizard.application_id)
        with self.assertRaisesRegex(UserError, "application ID"):
            wizard.action_save_credentials()

    def test_invalid_key(self):
        wizard = self.env["l10n_pl.eb.connect"].create(
            {
                "step": "credentials",
                "application_id": "new-app",
                "paste_key": True,
                "private_key": "not a key",
            }
        )
        with self.assertRaises(UserError):
            wizard.action_save_credentials()
        self.assertEqual(self.company.l10n_pl_eb_application_id, APP_ID)

    def test_key_rejected_by_enable_banking(self):
        _key, pem = _make_pem()
        wizard = self.env["l10n_pl.eb.connect"].create(
            {
                "step": "credentials",
                "application_id": "other-app",
                "paste_key": True,
                "private_key": pem,
            }
        )
        with (
            mock.patch(
                _wizard_class + "._get_aspsps",
                side_effect=EnableBankingError("401 Unauthorized", status=401),
            ),
            self.assertRaises(UserError),
        ):
            wizard.action_save_credentials()
        # The previous application stays.
        self.assertEqual(self.company.l10n_pl_eb_application_id, APP_ID)
        self.assertEqual(self.company.l10n_pl_eb_private_key, self.pem)

    def test_menu_and_dashboard(self):
        self.assertTrue(
            self.env.ref(
                "l10n_pl_account_statement_import_online_enablebanking."
                "menu_l10n_pl_eb_bank"
            ).parent_id
            == self.env.ref("account.menu_finance")
        )
        admin = self.env.ref("base.user_admin")
        # The "New" column of the tile menu needs full accounting features.
        admin.group_ids += self.env.ref("account.group_account_user")
        arch = (
            self.env["account.journal"]
            .with_user(admin)
            .get_views(
                [
                    (
                        self.env.ref(
                            "account.account_journal_dashboard_kanban_view"
                        ).id,
                        "kanban",
                    )
                ]
            )["views"]["kanban"]["arch"]
        )
        self.assertIn("action_l10n_pl_eb_connect", arch)
        self.assertIn("action_l10n_pl_eb_add_bank", arch)
        action = self.env["account.journal"]._l10n_pl_eb_reconcile_action()
        self.assertEqual(action["res_model"], "account.bank.statement.line")


@tagged("post_install", "-at_install")
class TestEnableBankingConnection(EnableBankingCommon):
    def test_connect_creates_journals(self):
        connection = self._connect(transactions=self.transactions)
        self.assertEqual(connection.state, "active")
        self.assertFalse(connection.auth_state)
        self.assertEqual(connection.session_id, "sess-1")
        self.assertEqual(
            connection.valid_until,
            fields.Datetime.to_datetime("2027-05-10 10:00:00"),
        )
        self.assertEqual(connection.journal_count, 2)
        journals = connection.provider_ids.journal_id
        pln = journals.filtered(
            lambda j: j.bank_account_id.sanitized_acc_number == OWN_IBAN
        )
        eur = journals - pln
        self.assertEqual(pln.name, "Konto firmowe 0001")
        self.assertEqual(pln.currency_id or self.company.currency_id, self.currency_pln)
        self.assertEqual(eur.currency_id, self.env.ref("base.EUR"))
        for journal in journals:
            self.assertEqual(journal.type, "bank")
            self.assertEqual(journal.bank_statements_source, "online")
            provider = journal.online_bank_statement_provider_id
            self.assertEqual(provider.service, "l10n_pl_enablebanking")
            self.assertEqual(provider.l10n_pl_eb_connection_id, connection)
            self.assertEqual(provider.statement_creation_mode, "monthly")
        # The last month was downloaded right away (pending line skipped).
        lines = self.env["account.bank.statement.line"].search(
            [("journal_id", "=", pln.id)], order="date"
        )
        self.assertEqual(sorted(lines.mapped("amount")), [-246.0, 1230.0])
        self.assertEqual(
            lines.filtered(lambda line: line.amount > 0).partner_name,
            "Klient Przykładowy sp. z o.o.",
        )

    def test_connect_sets_opening_balance(self):
        """The first statement starts from the bank's balance before the
        downloaded month, so the journal ends on the bank's balance."""
        balances = [
            {
                "balance_type": "ITBD",
                "balance_amount": {"amount": "5000.00", "currency": "PLN"},
            }
        ]
        with mock.patch(
            _provider_class + "._l10n_pl_eb_request_balances", return_value=balances
        ):
            connection = self._connect(transactions=self.transactions)
        journal = self._pln_provider(connection).journal_id
        self.assertAlmostEqual(journal.current_statement_balance, 5000.0)
        first = self.env["account.bank.statement"].search(
            [("journal_id", "=", journal.id)], order="first_line_index", limit=1
        )
        self.assertAlmostEqual(first.balance_start, 5000.0 - 1230.0 + 246.0)

    def test_connect_fills_unused_bank_journal(self):
        Journal = self.env["account.journal"]
        used = Journal.create(
            {
                "name": "Kasa w banku",
                "type": "bank",
                "code": "UBK1",
                "sequence": 0,
                "currency_id": self.currency_pln.id,
            }
        )
        self.env["account.bank.statement.line"].create(
            {"journal_id": used.id, "amount": 10.0, "payment_ref": "x"}
        )
        unused = Journal.create(
            {
                "name": "Bank",
                "type": "bank",
                "code": "UBK2",
                "sequence": 1,
                "currency_id": self.currency_pln.id,
            }
        )
        connection = self._connect()
        journals = connection.provider_ids.journal_id
        self.assertIn(unused, journals)
        self.assertNotIn(used, journals)
        self.assertEqual(unused.bank_account_id.sanitized_acc_number, OWN_IBAN)
        self.assertEqual(unused.bank_statements_source, "online")
        # the EUR account still gets its own journal
        self.assertEqual(connection.journal_count, 2)

    def test_connect_reuses_journal_with_same_iban(self):
        partner_bank = self.env["res.partner.bank"].create(
            {
                "acc_number": OWN_IBAN,
                "partner_id": self.company.partner_id.id,
                "company_id": self.company.id,
            }
        )
        journal = self.env["account.journal"].create(
            {
                "name": "mBank",
                "type": "bank",
                "code": "MBK",
                "currency_id": self.currency_pln.id,
                "bank_account_id": partner_bank.id,
            }
        )
        connection = self._connect()
        self.assertIn(journal, connection.provider_ids.journal_id)
        self.assertEqual(connection.journal_count, 2)
        # the bank name is already in the journal name
        self.assertEqual(
            journal.online_bank_statement_provider_id.name, "mBank – Enable Banking"
        )
        self.assertEqual(
            journal.online_bank_statement_provider_id.l10n_pl_eb_account_uid,
            "uid-pln",
        )

    def test_connect_from_journal_tile(self):
        journal = self.env["account.journal"].create(
            {
                "name": "Bank",
                "type": "bank",
                "code": "BNKX",
                "currency_id": self.currency_pln.id,
            }
        )
        connection = self._connect(journal=journal)
        self.assertEqual(connection.target_journal_id, journal)
        self.assertEqual(journal.bank_account_id.sanitized_acc_number, OWN_IBAN)
        self.assertIn(journal, connection.provider_ids.journal_id)
        self.assertEqual(
            journal.online_bank_statement_provider_id.name,
            "Bank – Enable Banking (mBank)",
        )

    def test_upstream_labels_translated(self):
        """i18n_extra/pl.po translates the labels of the OCA base module."""
        if not self.env["res.lang"]._lang_get("pl_PL"):
            self.skipTest("Polish is not installed")
        self.env["ir.module.module"]._load_module_terms(
            [
                "account_statement_import_online",
                "l10n_pl_account_statement_import_online_enablebanking",
            ],
            ["pl_PL"],
        )
        Provider = self.env["online.bank.statement.provider"].with_context(lang="pl_PL")
        fields_info = Provider.fields_get(["interval_number", "interval_type"])
        self.assertEqual(fields_info["interval_number"]["string"], "Co ile pobierać")
        self.assertIn(("hours", "Godziny"), fields_info["interval_type"]["selection"])

    def test_add_second_bank(self):
        first = self._connect()
        second = self._connect(
            accounts=[
                {
                    "uid": "uid-2",
                    "account_id": {"iban": "PL20000000000000000000000003"},
                    "name": "Konto",
                }
            ]
        )
        self.assertNotEqual(first, second)
        self.assertEqual(second.journal_count, 1)
        self.assertEqual(
            self.env["account.journal"].search_count(
                [("online_bank_statement_provider", "=", "l10n_pl_enablebanking")]
            ),
            3,
        )

    def test_account_without_number_skipped(self):
        connection = self._connect(accounts=[{"uid": "x", "account_id": {}}])
        self.assertEqual(connection.journal_count, 0)
        self.assertIn("Skipped", connection.message_ids[0].body)

    def test_pull_no_duplicates(self):
        provider = self._pln_provider(self._connect())
        since = fields.Date.today() - timedelta(days=10)
        until = fields.Date.today()
        self.assertEqual(len(self._pull(provider, since, until)), 2)
        self.assertEqual(len(self._pull(provider, since, until)), 2)

    def test_expired_consent(self):
        connection = self._connect()
        connection.valid_until = fields.Datetime.now() - timedelta(days=1)
        provider = self._pln_provider(connection)
        with self.assertRaises(UserError):
            self._pull(
                provider,
                fields.Date.today() - timedelta(days=10),
                fields.Date.today(),
            )

    def test_expired_consent_scheduled_keeps_gap(self):
        connection = self._connect()
        provider = self._pln_provider(connection)
        last_run = fields.Datetime.now() - timedelta(days=5)
        provider.last_successful_run = last_run
        connection.valid_until = fields.Datetime.now() - timedelta(days=1)
        # The scheduled pull logs the expired consent with its traceback.
        with mute_logger(_online_provider_logger):
            provider.with_context(scheduled=True)._pull(last_run, fields.Datetime.now())
        self.assertEqual(connection.state, "expired")
        # Scheduled pulls stop and the missing days are not skipped.
        self.assertFalse(provider.active)
        self.assertLessEqual(provider.last_successful_run, last_run)
        # Renewing the consent reactivates the account.
        with mock.patch(
            _client_class + ".start_authorization",
            return_value={"url": "https://bank.example/auth"},
        ):
            connection.action_reconnect()
        with (
            mock.patch(_client_class + ".create_session", return_value=self._session()),
            mock.patch(
                _provider_class + "._l10n_pl_eb_request_transactions", return_value=[]
            ),
        ):
            connection._finish_authorization("code2")
        self.assertTrue(provider.active)
        self.assertEqual(connection.state, "active")
        self.assertEqual(connection.journal_count, 2)

    def test_expiry_warning(self):
        connection = self._connect()
        connection.valid_until = fields.Datetime.now() + timedelta(days=3)
        provider = self._pln_provider(connection)
        self._pull(
            provider, fields.Date.today() - timedelta(days=10), fields.Date.today()
        )
        self.assertTrue(connection.expiry_warned)

    def test_disconnect(self):
        connection = self._connect()
        with mock.patch(_client_class + ".delete_session") as delete:
            connection.action_disconnect()
        delete.assert_called_once_with("sess-1")
        self.assertEqual(connection.state, "revoked")
        provider = self._pln_provider(connection)
        self.assertFalse(provider.active)
        # The archived account is not pulled any more.
        self.assertFalse(
            self._pull(
                provider,
                fields.Date.today() - timedelta(days=10),
                fields.Date.today(),
            )
        )

    def test_same_iban_two_currencies(self):
        connection = self._connect(
            accounts=[
                {
                    "uid": "u-pln",
                    "account_id": {"iban": OWN_IBAN},
                    "name": "Multi",
                    "currency": "PLN",
                },
                {
                    "uid": "u-eur",
                    "account_id": {"iban": OWN_IBAN},
                    "name": "Multi",
                    "currency": "EUR",
                },
            ]
        )
        self.assertEqual(connection.journal_count, 2)
        self.assertEqual(
            sorted(connection.provider_ids.mapped("l10n_pl_eb_account_uid")),
            ["u-eur", "u-pln"],
        )

    def test_archived_partner_bank_reused(self):
        self.env["res.partner.bank"].create(
            {
                "acc_number": EUR_IBAN,
                "partner_id": self.company.partner_id.id,
                "active": False,
            }
        )
        connection = self._connect()
        self.assertEqual(connection.journal_count, 2)

    def test_connect_without_bank_account_rights(self):
        """An accounting manager who may not create bank accounts (as with
        account_payment_order installed) still gets the accounts linked."""
        manager = self.env["res.users"].create(
            {
                "name": "Owner",
                "login": "eb_owner",
                "company_id": self.company.id,
                "company_ids": [(6, 0, self.company.ids)],
                "group_ids": [
                    (
                        6,
                        0,
                        [
                            self.env.ref("account.group_account_manager").id,
                            self.env.ref("account.group_account_user").id,
                            self.env.ref("account.group_validate_bank_account").id,
                        ],
                    )
                ],
            }
        )
        with self.assertRaises(AccessError):
            self.env["res.partner.bank"].with_user(manager).create(
                {
                    "acc_number": "PL20000000000000000000000099",
                    "partner_id": self.company.partner_id.id,
                }
            )
        unused = self.env["account.journal"].create(
            {
                "name": "Bank",
                "code": "UBK9",
                "type": "bank",
                "company_id": self.company.id,
                "sequence": 1,
                "currency_id": self.env.ref("base.PLN").id,
            }
        )
        self.env = self.env(user=manager)
        connection = self._connect()
        self.assertTrue(unused.bank_account_id)
        self.assertEqual(
            connection.journal_count, 2, connection.sudo().message_ids.mapped("body")
        )
        self.assertEqual(
            self._pln_provider(connection).journal_id.bank_account_id.partner_id,
            self.company.partner_id,
        )

    def test_access_rights(self):
        connection = self._connect()
        accountant = self.env["res.users"].create(
            {
                "name": "Accountant",
                "login": "eb_accountant",
                "group_ids": [(6, 0, [self.env.ref("account.group_account_user").id])],
            }
        )
        as_accountant = connection.with_user(accountant)
        self.assertEqual(as_accountant.journal_count, 2)
        with self.assertRaises(AccessError):
            as_accountant.action_disconnect()
        with self.assertRaises(AccessError):
            self.env["l10n_pl.eb.connect"].with_user(accountant).create({})
        other_company = self.env["res.company"].create({"name": "Other"})
        manager = self.env["res.users"].create(
            {
                "name": "Manager",
                "login": "eb_manager",
                "company_id": other_company.id,
                "company_ids": [(6, 0, other_company.ids)],
                "group_ids": [
                    (6, 0, [self.env.ref("account.group_account_manager").id])
                ],
            }
        )
        self.assertFalse(
            self.env["l10n_pl.eb.connection"].with_user(manager).search([])
        )
        with self.assertRaises(AccessError):
            self.env["l10n_pl.eb.connect"].with_user(manager).create(
                {"company_id": self.company.id}
            )


@tagged("post_install", "-at_install")
class TestEnableBankingCallback(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _patch_optional_calls(cls)

    def test_callback(self):
        _key, pem = _make_pem()
        self.env.company.write(
            {"l10n_pl_eb_application_id": APP_ID, "l10n_pl_eb_private_key": pem}
        )
        connection = self.env["l10n_pl.eb.connection"].create(
            {"name": "mBank", "aspsp_name": "mBank", "aspsp_country": "PL"}
        )
        connection.auth_state = "s3cr3t"
        session = EnableBankingCommon._session(
            [{"uid": "u1", "account_id": {"iban": OWN_IBAN}, "name": "Konto"}]
        )
        self.authenticate("admin", "admin")
        with (
            mock.patch(_client_class + ".create_session", return_value=session),
            mock.patch(
                _provider_class + "._l10n_pl_eb_request_transactions",
                return_value=[],
            ),
        ):
            # A wrong state does nothing.
            self.url_open("/l10n_pl_enablebanking/callback?state=bad&code=x")
            self.assertEqual(connection.state, "draft")
            response = self.url_open(
                "/l10n_pl_enablebanking/callback?state=s3cr3t&code=abc",
                allow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            f"l10n_pl_eb_connection_action/{connection.id}",
            response.headers["Location"],
        )
        connection.invalidate_recordset()
        self.assertEqual(connection.state, "active")
        self.assertEqual(connection.journal_count, 1)


VAT_IBAN = "PL63000000000000000000000005"


@tagged("post_install", "-at_install")
class TestEnableBankingDetails(EnableBankingCommon):
    """Descriptions, raw data, refresh and split payment transfers."""

    def setUp(self):
        super().setUp()
        self.company.partner_id.lang = "en_US"
        self.provider = self._pln_provider(self._connect())
        self.journal = self.provider.journal_id
        self.since = fields.Date.today() - timedelta(days=10)
        self.until = fields.Date.today()

    def _rich(self):
        return {
            "entry_reference": "R1",
            "transaction_amount": {"amount": "99.00", "currency": "PLN"},
            "credit_debit_indicator": "DBIT",
            "status": "BOOK",
            "booking_date": self.day1,
            "creditor": {
                "name": "Orlen  SA",
                "postal_address": {"town_name": "Plock", "post_code": "09-411"},
            },
            "creditor_agent": {"bic_fi": "PKOPPLPW"},
            "creditor_account_additional_identification": [
                {"identification": "1234 5678", "scheme_name": "CPAN"}
            ],
            "bank_transaction_code": {
                "code": "PMNT",
                "sub_code": "CCRD",
                "description": "Card payment",
            },
            "merchant_category_code": "5541",
            "reference_number": "RF18539007547034",
            "note": "Stacja 123",
            "remittance_information": [],
        }

    def test_rich_transaction(self):
        self.transactions = [self._rich()]
        line = self._pull(self.provider, self.since, self.until)
        self.assertEqual(line.payment_ref, "Orlen SA")
        self.assertEqual(line.partner_name, "Orlen SA")
        self.assertEqual(line.account_number, "12345678")
        self.assertEqual(line.ref, "RF18539007547034")
        self.assertEqual(line.transaction_type, "PMNT CCRD Card payment")
        note = str(line.narration)
        for text in ("PKOPPLPW", "5541", "Stacja 123", "09-411 Plock", "Card payment"):
            self.assertIn(text, note)
        self.assertEqual(json.loads(line.raw_data)["merchant_category_code"], "5541")

    def test_title_and_fallbacks(self):
        empty = {
            "transaction_id": "X1",
            "transaction_amount": {"amount": "5.00", "currency": "PLN"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": self.day1,
        }
        note_only = dict(empty, transaction_id="X2", note="Odsetki")
        code_only = dict(
            empty,
            transaction_id="X3",
            bank_transaction_code={"description": "Interest"},
        )
        structured = dict(
            empty,
            transaction_id="X4",
            remittance_information_structured="RF71 1234",
        )
        self.transactions = [empty, note_only, code_only, structured]
        lines = self._pull(self.provider, self.since, self.until)
        refs = {json.loads(line.raw_data)["transaction_id"]: line for line in lines}
        self.assertEqual(refs["X1"].payment_ref, f"Credit {self.day1}")
        self.assertEqual(refs["X2"].payment_ref, "Odsetki")
        self.assertEqual(refs["X3"].payment_ref, "Interest")
        self.assertEqual(refs["X4"].payment_ref, "RF71 1234")

    def test_split_payment_message_is_not_a_transfer(self):
        """A supplier paid with split payment keeps its own title."""
        self.journal.l10n_pl_vat_account_number = VAT_IBAN
        lines = self._pull(self.provider, self.since, self.until)
        paid = lines.filtered(lambda line: line.amount < 0)
        self.assertIn("/VAT/46,00/", paid.payment_ref)
        self.assertNotEqual(paid.transaction_type, "MPP")

    def _vat_transfer(self, indicator="DBIT", **extra):
        side = "creditor" if indicator == "DBIT" else "debtor"
        return dict(
            {
                "entry_reference": f"V-{indicator}",
                "transaction_amount": {"amount": "230.00", "currency": "PLN"},
                "credit_debit_indicator": indicator,
                "status": "BOOK",
                "booking_date": self.day2,
                f"{side}_account": {"iban": VAT_IBAN},
            },
            **extra,
        )

    def test_vat_transfer_by_account_with_mirror(self):
        self.journal.l10n_pl_vat_account_number = VAT_IBAN.replace("PL", "PL ")
        self.journal.action_l10n_pl_vat_create_journal()
        vat_journal = self.journal.l10n_pl_vat_journal_id
        self.assertEqual(vat_journal.type, "bank")
        self.assertEqual(vat_journal.bank_account_id.sanitized_acc_number, VAT_IBAN)
        model = self.env["account.reconcile.model"].search(
            [("match_label_param", "=", "(MPP)")]
        )
        self.assertEqual(model.match_journal_ids, self.journal | vat_journal)
        self.journal.l10n_pl_vat_auto_reconcile = True
        self.transactions = [self._vat_transfer(), self._vat_transfer("CRDT")]
        lines = self._pull(self.provider, self.since, self.until)
        self.assertEqual(set(lines.mapped("transaction_type")), {"MPP"})
        self.assertEqual(
            set(lines.mapped("payment_ref")),
            {
                "Transfer to the VAT account (MPP)",
                "Transfer from the VAT account (MPP)",
            },
        )
        mirrors = self.env["account.bank.statement.line"].search(
            [("journal_id", "=", vat_journal.id)]
        )
        self.assertEqual(sorted(mirrors.mapped("amount")), [-230.0, 230.0])
        # booked on the internal transfer account: no income, no expense
        transfer = self.company.transfer_account_id
        for line in lines | mirrors:
            self.assertTrue(line.is_reconciled)
            _liquidity, _suspense, other = line._seek_for_lines()
            self.assertEqual(other.account_id, transfer)
        moves = (lines | mirrors).move_id
        self.assertFalse(
            moves.line_ids.filtered(
                lambda aml: aml.account_id.internal_group in ("income", "expense")
            )
        )
        self.assertAlmostEqual(
            sum(
                moves.line_ids.filtered(lambda a: a.account_id == transfer).mapped(
                    "balance"
                )
            ),
            0.0,
        )
        # a second download creates neither lines nor mirrors
        self._pull(self.provider, self.since, self.until)
        self.assertEqual(
            self.env["account.bank.statement.line"].search_count(
                [("journal_id", "=", vat_journal.id)]
            ),
            2,
        )

    def test_vat_transfer_by_rule(self):
        self.journal.l10n_pl_vat_match_regex = r'"code": "VATT"'
        transfer = self._vat_transfer(bank_transaction_code={"code": "VATT"})
        del transfer["creditor_account"]
        self.transactions = [transfer]
        line = self._pull(self.provider, self.since, self.until)
        self.assertEqual(line.transaction_type, "MPP")
        # without a VAT journal nothing else is created
        self.assertFalse(line.is_reconciled)

    def test_invalid_rule(self):
        with self.assertRaises(ValidationError):
            self.journal.l10n_pl_vat_match_regex = "(unclosed"

    def test_refresh_descriptions(self):
        poor = {
            "entry_reference": "Q1",
            "transaction_amount": {"amount": "50.00", "currency": "PLN"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": self.day1,
        }
        self.transactions = [poor, self.transactions[1]]
        lines = self._pull(self.provider, self.since, self.until)
        poor_line = lines.filtered(lambda line: line.amount == 50)
        self.assertEqual(poor_line.payment_ref, f"Credit {self.day1}")
        paid = lines - poor_line
        paid.move_id.line_ids.filtered(
            lambda aml: aml.account_id == self.journal.suspense_account_id
        ).with_context(
            skip_account_move_synchronization=True, skip_readonly_check=True
        ).write({"account_id": self.company.transfer_account_id.id})
        self.assertTrue(paid.is_reconciled)
        old_ref = paid.payment_ref
        rich = dict(
            poor,
            debtor={"name": "Jan Kowalski"},
            remittance_information=["Zaliczka 5/2026"],
        )
        changed_paid = dict(self.transactions[1], remittance_information=["inny"])
        wizard = self.env["l10n_pl.eb.refresh"].create(
            {
                "connection_id": self.provider.l10n_pl_eb_connection_id.id,
                "date_since": self.since,
                "date_until": self.until,
            }
        )
        with mock.patch(
            _provider_class + "._l10n_pl_eb_request_transactions",
            return_value=[rich, changed_paid, dict(poor, entry_reference="NEW")],
        ):
            wizard.action_refresh()
        self.assertEqual(poor_line.payment_ref, "Zaliczka 5/2026")
        self.assertEqual(poor_line.partner_name, "Jan Kowalski")
        self.assertEqual(
            json.loads(poor_line.raw_data)["debtor"]["name"], "Jan Kowalski"
        )
        # reconciled line: booking untouched, raw data saved
        self.assertEqual(paid.payment_ref, old_ref)
        self.assertIn("inny", paid.raw_data)
        # no line added
        self.assertEqual(
            self.env["account.bank.statement.line"].search_count(
                [("journal_id", "=", self.journal.id)]
            ),
            2,
        )
        self.assertIn(
            "1 lines updated",
            self.provider.l10n_pl_eb_connection_id.message_ids[0].body,
        )

    def test_details_balances_and_account_data(self):
        thin = {
            "transaction_id": "TX9",
            "transaction_amount": {"amount": "77.00", "currency": "PLN"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": self.day1,
        }
        self.transactions = [thin]
        self.details_mock.return_value = {
            "transaction_id": "TX9",
            "debtor": {"name": "Anna Nowak"},
            "remittance_information": ["Wiazanka slubna"],
        }
        self.balances_mock.return_value = [
            {
                "name": "Dostepne",
                "balance_amount": {"amount": "500.00", "currency": "PLN"},
                "balance_type": "ITAV",
            },
            {
                "name": "Ksiegowe",
                "balance_amount": {"amount": "1234.56", "currency": "PLN"},
                "balance_type": "CLBD",
            },
        ]
        line = self._pull(self.provider, self.since, self.until)
        self.details_mock.assert_called_with("TX9")
        self.assertEqual(line.payment_ref, "Wiazanka slubna")
        self.assertEqual(line.partner_name, "Anna Nowak")
        self.assertIn("TX9", line.unique_import_id)
        self.assertEqual(
            json.loads(line.raw_data)["_details"]["debtor"]["name"], "Anna Nowak"
        )
        self.assertEqual(self.provider.l10n_pl_eb_balance, 1234.56)
        self.assertEqual(self.provider.l10n_pl_eb_balance_type, "CLBD")
        self.assertIn("Dostepne", self.provider.l10n_pl_eb_balances_data)
        account_data = json.loads(self.provider.l10n_pl_eb_account_data)
        self.assertEqual(account_data["session"]["uid"], "uid-pln")

    def test_details_error_does_not_stop_download(self):
        self.transactions = [
            {
                "transaction_id": "TX10",
                "transaction_amount": {"amount": "3.00", "currency": "PLN"},
                "credit_debit_indicator": "DBIT",
                "status": "BOOK",
                "booking_date": self.day1,
            }
        ]
        self.details_mock.side_effect = EnableBankingError("boom", status=404)
        self.balances_mock.side_effect = EnableBankingError("boom", status=500)
        try:
            line = self._pull(self.provider, self.since, self.until)
        finally:
            self.details_mock.side_effect = None
            self.balances_mock.side_effect = None
        self.assertEqual(line.payment_ref, f"Debit {self.day1}")

    def _empty(self, ref, amount, indicator, day=None):
        return {
            "entry_reference": ref,
            "transaction_amount": {"amount": amount, "currency": "PLN"},
            "credit_debit_indicator": indicator,
            "status": "BOOK",
            "booking_date": day or self.day1,
            "remittance_information": [""],
        }

    def _paid_to(self, ref, amount, name, iban, title="", day=None):
        return dict(
            self._empty(ref, amount, "DBIT", day),
            creditor={"name": name},
            creditor_account={"iban": iban} if iban else None,
            remittance_information=[title] if title else [],
        )

    def test_split_payment_from_vat_account(self):
        """Money moved from the VAT account: for an outgoing split payment
        without a title (Bank Millennium) and for ZUS or the tax office."""
        self.journal.l10n_pl_vat_empty_lines = True
        zus = "PL12600000020260015792207681"
        tax_office = "PL17101000712222579220768100"
        day3 = fields.Date.to_string(fields.Date.today() - timedelta(days=2))
        self.transactions = [
            # outgoing split payment: no title, a fee and the VAT moved back
            self._paid_to(
                "O1", "6150.00", "Restruktor", "PL61109010140000071219812874"
            ),
            dict(
                self._empty("O2", "10.00", "DBIT"),
                remittance_information=["OUTGOING VAT TRANSFER"],
            ),
            self._empty("O3", "1150.00", "CRDT"),
            # ZUS paid in full from the VAT account
            self._paid_to("Z1", "3377.60", "ZUS", zus, "Skladka ZUS", self.day2),
            self._empty("Z2", "3377.60", "CRDT", self.day2),
            # tax office paid partly from the VAT account
            self._paid_to("U1", "3263.28", "Urzad Skarbowy", tax_office, "PIT", day3),
            self._empty("U2", "2946.64", "CRDT", day3),
            # a card payment without a title is no split payment
            self._paid_to("K1", "39.96", "Sambor", False, day=day3),
        ]
        lines = self._pull(self.provider, self.since, self.until)
        by_ref = {json.loads(line.raw_data)["entry_reference"]: line for line in lines}
        self.assertEqual(
            by_ref["O1"].payment_ref, "Outgoing VAT transfer (split payment)"
        )
        self.assertEqual(by_ref["O1"].partner_name, "Restruktor")
        self.assertNotEqual(by_ref["O1"].transaction_type, "MPP")
        for ref in ("O3", "Z2", "U2"):
            self.assertEqual(
                by_ref[ref].payment_ref, "Transfer from the VAT account (MPP)", ref
            )
            self.assertEqual(by_ref[ref].transaction_type, "MPP")
        self.assertIn("Restruktor", str(by_ref["O3"].narration))
        self.assertIn("Skladka ZUS", str(by_ref["Z2"].narration))
        self.assertEqual(by_ref["Z1"].payment_ref, "Skladka ZUS")
        self.assertEqual(by_ref["K1"].payment_ref, "Sambor")
        self.assertEqual(by_ref["O2"].payment_ref, "OUTGOING VAT TRANSFER")

    def test_split_payment_pairs_in_empty_lines(self):
        outgoing = dict(
            self.transactions[1],
            booking_date=self.day2,
            remittance_information=["/VAT/46,00/IDC/5260250995/INV/FZ 7/10"],
        )
        self.transactions = [
            self._empty("G1|2", "2460.00", "CRDT"),  # customer, gross
            self._empty("G1|1", "460.00", "DBIT"),  # its VAT, 23%
            self._empty("G2|2", "1080.00", "CRDT"),  # customer, 8%
            self._empty("G2|1", "80.00", "DBIT"),
            self._empty("E1|2", "300.00", "CRDT"),  # equal amounts: unknown
            self._empty("E1|1", "300.00", "DBIT"),
            self._empty("T1|2", "46.00", "CRDT", self.day2),  # VAT of outgoing
            outgoing,
        ]
        lines = self._pull(self.provider, self.since, self.until)
        raw = {line: json.loads(line.raw_data) for line in lines}
        by_ref = {
            data.get("entry_reference") or data.get("transaction_id"): line
            for line, data in raw.items()
        }
        # without the option: neutral descriptions only
        self.assertEqual(by_ref["G1|1"].payment_ref, f"Debit {self.day1}")
        self.assertEqual(by_ref["G1|2"].payment_ref, f"Credit {self.day1}")
        self.journal.l10n_pl_vat_empty_lines = True
        wizard = self.env["l10n_pl.eb.refresh"].create(
            {
                "connection_id": self.provider.l10n_pl_eb_connection_id.id,
                "date_since": self.since,
                "date_until": self.until,
            }
        )
        with mock.patch(
            _provider_class + "._l10n_pl_eb_request_transactions",
            return_value=self.transactions,
        ):
            wizard.action_refresh()
        for ref in ("G1|1", "G2|1"):
            self.assertEqual(
                by_ref[ref].payment_ref, "Transfer to the VAT account (MPP)"
            )
            self.assertEqual(by_ref[ref].transaction_type, "MPP")
        for ref in ("G1|2", "G2|2"):
            self.assertEqual(by_ref[ref].payment_ref, "Split payment receipt (gross)")
            self.assertEqual(by_ref[ref].transaction_type, "MPP brutto")
            self.assertFalse(by_ref[ref].partner_name)
        self.assertIn("460.00", str(by_ref["G1|2"].narration))
        self.assertEqual(by_ref["E1|2"].payment_ref, f"Credit {self.day1}")
        self.assertEqual(by_ref["E1|1"].payment_ref, f"Debit {self.day1}")
        self.assertEqual(
            by_ref["T1|2"].payment_ref, "Transfer from the VAT account (MPP)"
        )
        self.assertIn("/VAT/46,00/", by_ref["T2"].payment_ref)
        # a VAT journal mirrors only the transfers, not the gross receipts
        self.journal.action_l10n_pl_vat_create_journal()
        self.journal._l10n_pl_vat_process_transfers()
        mirrors = self.env["account.bank.statement.line"].search(
            [("journal_id", "=", self.journal.l10n_pl_vat_journal_id.id)]
        )
        self.assertEqual(sorted(mirrors.mapped("amount")), [-46.0, 80.0, 460.0])
