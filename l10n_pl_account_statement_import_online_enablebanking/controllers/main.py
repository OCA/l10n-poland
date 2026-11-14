# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import werkzeug

from odoo import http
from odoo.http import request

CONNECTIONS_URL = (
    "/odoo/action-l10n_pl_account_statement_import_online_enablebanking."
    "l10n_pl_eb_connection_action"
)


class EnableBankingController(http.Controller):
    @http.route("/l10n_pl_enablebanking/callback", type="http", auth="user", csrf=False)
    def l10n_pl_eb_callback(self, state=None, code=None, error=None, **kwargs):
        env = request.env
        if not env.user.has_group("account.group_account_manager") or not state:
            return werkzeug.utils.redirect(CONNECTIONS_URL, 303)
        # ``auth_state`` is readable by accounting managers only; the random
        # state also proves the redirect belongs to a consent started here.
        connection = env["l10n_pl.eb.connection"].search(
            [
                ("auth_state", "=", state),
                ("company_id", "in", env.user.company_ids.ids),
            ],
            limit=1,
        )
        if not connection:
            return werkzeug.utils.redirect(CONNECTIONS_URL, 303)
        connection = connection.with_company(connection.company_id)
        if error or not code:
            connection.sudo().auth_state = False
            connection.message_post(
                body=env._(
                    "The bank returned an error (%(error)s %(desc)s).",
                    error=error or "no code",
                    desc=kwargs.get("error_description") or "",
                )
            )
        else:
            connection._finish_authorization(code)
        return werkzeug.utils.redirect(f"{CONNECTIONS_URL}/{connection.id}", 303)
