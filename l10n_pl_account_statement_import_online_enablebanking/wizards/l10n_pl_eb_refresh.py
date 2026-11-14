# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from datetime import datetime, time, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class EnableBankingRefresh(models.TransientModel):
    _name = "l10n_pl.eb.refresh"
    _description = "Download the descriptions of imported lines again"
    _check_company_auto = True

    connection_id = fields.Many2one(
        "l10n_pl.eb.connection", required=True, ondelete="cascade"
    )
    company_id = fields.Many2one(related="connection_id.company_id")
    date_since = fields.Date(string="From", required=True)
    date_until = fields.Date(
        string="To", required=True, default=fields.Date.context_today
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        connection = self.env["l10n_pl.eb.connection"].browse(
            res.get("connection_id") or self.env.context.get("active_id")
        )
        if connection and "date_since" in fields_list:
            first = self.env["account.bank.statement.line"].search(
                [("journal_id", "in", connection.sudo().provider_ids.journal_id.ids)],
                order="date asc",
                limit=1,
            )
            res["date_since"] = first.date or fields.Date.context_today(self) - (
                timedelta(days=30)
            )
        return res

    def action_refresh(self):
        self.ensure_one()
        connection = self.connection_id
        connection.check_access("write")
        if self.date_since > self.date_until:
            raise UserError(self.env._("The start date is after the end date."))
        since = datetime.combine(self.date_since, time.min)
        until = datetime.combine(self.date_until + timedelta(days=1), time.min)
        providers = connection.sudo().provider_ids.filtered("active")
        updated, kept, missing = providers._l10n_pl_eb_refresh_lines(since, until)
        connection.sudo().message_post(
            body=self.env._(
                "Descriptions downloaded again for %(since)s - %(until)s: "
                "%(updated)s lines updated, %(kept)s reconciled lines kept "
                "(only raw data saved), %(missing)s transactions not "
                "imported yet (they come with the next download).",
                since=fields.Date.to_string(self.date_since),
                until=fields.Date.to_string(self.date_until),
                updated=updated,
                kept=kept,
                missing=missing,
            )
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": self.env._(
                    "%(updated)s lines updated, %(kept)s reconciled lines kept.",
                    updated=updated,
                    kept=kept,
                ),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
