This module adds [Enable Banking](https://enablebanking.com) as an online bank
statements provider. Enable Banking gives read access to bank accounts through
the PSD2 account information APIs of many European banks. The module was tested
with Bank Millennium (Poland) and Revolut; the list of banks shown in the wizard
comes from Enable Banking.

One *Connect bank* button guides you through the Enable Banking application,
lets you pick the bank, and creates a bank journal for every account the bank
returns. Transactions are then pulled on schedule into bank statements, without
any file import. Your bank login never reaches Odoo: you authorize the access on the
bank's own page, and the consent is valid for up to 180 days (depending on
the bank).

The module uses its own technical names (service `l10n_pl_enablebanking`, fields
`l10n_pl_eb_*`, callback `/l10n_pl_enablebanking/callback`), so it can be installed next to
the independent `account_statement_import_online_enablebanking` module by Futural and RockIT.
