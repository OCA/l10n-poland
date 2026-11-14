Generates Elixir-O files from payment orders. Elixir-O is a common format for
importing domestic PLN transfers into Polish business e-banking. Files made
with this format were imported successfully in a Polish cooperative bank;
banks differ in details, so check your bank's import description before the
first real payment.

The module also supports the Polish split payment mechanism (MPP, mechanizm
podzielonej płatności): the transfer gets the `/VAT/…/IDC/…/INV/…` message and
transaction code 53, so the bank sends the VAT part to the supplier's VAT
account.

Split payment follows the invoice. For vendor bills imported from KSeF
(module `l10n_pl_edi`) the flag is taken from the invoice (field P_18A,
"mechanizm podzielonej płatności"), and the supplier bank account from the
invoice payment details (NrRB). The module never ticks split payment by the
amount alone.
