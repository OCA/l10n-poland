1. Vendor bills imported from KSeF come with *Split payment (MPP)* ticked when
   the supplier marked the invoice with split payment. A bank account found
   on the invoice that the supplier did not use before is added as not
   trusted, so check it before you pay.
2. For other bills tick *Split payment (MPP)* yourself. A PLN bill above
   15 000 PLN gross with VAT from a Polish supplier shows a reminder to check
   whether split payment applies (it is mandatory for goods and services from
   annex 15 to the VAT act).
3. Add bills to a payment order, confirm it and click *Generate payment
   file*. A split payment transfer always pays one invoice; for a partial
   payment the VAT part is proportional and can be edited on the payment line.
4. Import the file in your bank's e-banking and authorize the transfers there.
   The bank authorization (token, mobile app or qualified signature) stays
   with you; Odoo never sends money by itself.
