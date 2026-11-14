Everything is done from *Invoicing > Bank > Connect bank* (also the *Connect
bank* button on a bank tile of the dashboard, or *Add bank* in its menu).

The wizard first asks whether you already have an Enable Banking application.

**No:** it shows the steps:

1. Create a free account at <https://enablebanking.com/sign-in/>.
2. In *API applications* register an application: *Production* for your real
   accounts or *Sandbox* for test banks; keep the default key option; as
   *Allowed redirect URL* paste the address shown in the wizard
   (`https://<your-odoo>/l10n_pl_enablebanking/callback`). Production also
   requires a Privacy URL and Terms URL.
3. Production: click *Activate by linking accounts* next to the application
   and link your accounts in every bank (restricted mode). Permanent use in a
   company requires a contract with Enable Banking.

**Yes:** upload the downloaded `.pem` file. Its name is the application ID,
which is filled in automatically. Odoo checks the key with Enable Banking and
stores it on the company (accounting managers only). It is entered once; it can
be changed later in *Settings > Bank & Cash > Enable Banking*.

Then choose the bank, log in on the bank page and confirm. Back in Odoo a bank
journal is created for every account returned by the bank (name, IBAN,
currency) and the last 30 days are downloaded. A journal that already has the
same IBAN is reused; the first account fills an unused bank journal (no IBAN,
no statement lines yet, e.g. the default *Bank*) instead of creating a new one.

The user who connects the bank must be an accounting manager with full
accounting rights (the *Accounting: Full access* level, which can create bank
statements). With invoicing rights only the connection is made, but the first
download fails with an access error shown in the provider's chatter.
