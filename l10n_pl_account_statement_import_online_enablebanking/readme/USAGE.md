The *Connect bank* wizard lists the banks of the company's country. Tick
*Banks from all countries* to see every bank available through Enable Banking,
for example Revolut or an account held abroad; the bank's country is shown in
the list and used for the consent.

New transactions are downloaded by the scheduled action *Pull Online Bank
Statements*, or at once with *Download now* on the bank tile of the dashboard.

*Bank > Bank connections* lists every bank consent with its accounts and
expiry date. Seven days before the consent expires a message is posted on the
connection; *Renew consent* logs in to the bank again and *Disconnect* ends the
access (journals and statements stay).

*Bank > To reconcile* opens the reconciliation screen of
`account_reconcile_oca` when it is installed, otherwise a list of unreconciled
bank lines.
