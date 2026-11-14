# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Elixir-O file (domestic PLN transfers imported into Polish e-banking), no ORM.

One record per transfer, tested with imports in a Polish cooperative bank::

    110,YYYYMMDD,amount_in_grosze,sender_sort_code,0,"sender NRB","recipient NRB",
    "sender name|address","recipient name|address",0,recipient_sort_code,
    "title","","","51"

- text fields are split into parts of 35 characters with ``|`` (4 parts at most);
- sort code = digits 3-10 of the NRB (bank settlement number);
- split payment (MPP): title ``/VAT/amount,gr/IDC/NIP/INV/invoice``, last field "53";
- records end with CRLF; Polish letters are folded to ASCII, which every bank
  accepts whatever code page is chosen at import.
"""

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

PL_FOLD = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


class ElixirError(ValueError):
    """Data that cannot go into an Elixir-O file. ``code`` and ``value`` let
    the caller show a translated message."""

    def __init__(self, message, code=None, value=""):
        super().__init__(message)
        self.code = code
        self.value = value


def fold(text):
    """Text safe for Elixir-O: no Polish letters, quotes, commas or control
    characters."""
    text = (text or "").translate(PL_FOLD)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.replace('"', "'").replace(",", " ").replace("|", "/")
    return re.sub(r"\s+", " ", text).strip()


def chunk35(text, parts=4):
    """Split safe text into parts of 35 characters joined with ``|``, at most
    ``parts`` parts (the rest is cut off)."""
    return "|".join([text[i : i + 35] for i in range(0, len(text), 35)][:parts])


def split35(text, parts=4):
    """Any text, made safe and split into parts of 35 characters."""
    return chunk35(fold(text), parts)


def join_lines(*lines):
    """Name and address lines, each line starting a new 35-character part."""
    parts = []
    for line in lines:
        line = fold(line)
        while line:
            parts.append(line[:35])
            line = line[35:]
    return "|".join(parts[:4])


def nrb(account):
    """26 NRB digits from an IBAN or NRB (spaces, dashes and the PL prefix are
    removed). Checks the check digits (ISO 13616, mod 97)."""
    digits = re.sub(r"[\s-]", "", account or "").upper()
    if digits.startswith("PL"):
        digits = digits[2:]
    if not re.fullmatch(r"\d{26}", digits):
        raise ElixirError(
            f'Account number "{account or ""}" is not a Polish NRB (26 digits).',
            "not_nrb",
            account or "",
        )
    if int(digits[2:] + "2521" + digits[:2]) % 97 != 1:  # PL = 25 21
        raise ElixirError(
            f'Account number "{account}" has wrong check digits.',
            "check_digits",
            account,
        )
    return digits


def grosze(amount):
    value = (Decimal(str(amount)) * 100).quantize(Decimal("1"), ROUND_HALF_UP)
    if value <= 0:
        raise ElixirError(
            f"The transfer amount must be positive ({amount}).", "amount", amount
        )
    return int(value)


def money_pl(amount):
    return "{:.2f}".format(
        Decimal(str(amount)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    ).replace(".", ",")


def mpp_title(vat_amount, nip, invoice_number, text=""):
    """Split payment title: /VAT/amount,gr/IDC/NIP/INV/invoice (optionally
    /TXT/text)."""
    nip_digits = re.sub(r"\D", "", nip or "")
    if len(nip_digits) != 10:
        raise ElixirError(
            f'Split payment needs the recipient NIP (10 digits), not "{nip or ""}".',
            "nip",
            nip or "",
        )
    title = (
        f"/VAT/{money_pl(vat_amount)}/IDC/{nip_digits}/INV/{fold(invoice_number)[:35]}"
    )
    if text:
        title += f"/TXT/{fold(text)[:33]}"
    return title


def record(
    execution_date,
    amount,
    sender_account,
    recipient_account,
    sender_name_lines,
    recipient_name_lines,
    title,
    mpp=False,
    vat_amount=None,
    recipient_nip=None,
    invoice_number=None,
):
    """One Elixir-O record without CRLF. ``execution_date`` is a date, a
    datetime or 'YYYYMMDD'."""
    day = (
        execution_date
        if isinstance(execution_date, str)
        else execution_date.strftime("%Y%m%d")
    )
    sender, recipient = nrb(sender_account), nrb(recipient_account)
    if mpp:
        title_text = chunk35(
            mpp_title(vat_amount or 0, recipient_nip, invoice_number or title)
        )
    else:
        title_text = split35(title)
    sender_name = join_lines(*sender_name_lines)
    recipient_name = join_lines(*recipient_name_lines)
    fields = [
        "110",
        day,
        str(grosze(amount)),
        sender[2:10],
        "0",
        f'"{sender}"',
        f'"{recipient}"',
        f'"{sender_name}"',
        f'"{recipient_name}"',
        "0",
        recipient[2:10],
        f'"{title_text}"',
        '""',
        '""',
        '"53"' if mpp else '"51"',
    ]
    return ",".join(fields)


def build_file(records):
    """The file: ASCII records, each ending with CRLF."""
    if not records:
        raise ElixirError("The payment order has no transfers.", "empty")
    return ("\r\n".join(records) + "\r\n").encode("ascii")
