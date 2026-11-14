1. Set the bank account (NRB/IBAN) on your bank journal and add the payment
   method *Elixir-O (Polish domestic transfers)* to its outgoing methods.
2. Create a payment mode with this payment method
   (*Invoicing > Configuration > Payment Modes*).
3. Suppliers need a bank account; split payment also needs their Polish NIP.
4. Users who import KSeF bills need the *Payments* group of
   `account_payment_order` to add a supplier bank account found on the
   invoice. Without it the bill is still imported, only without the account.
