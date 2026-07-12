"""CSV / XLSX of the saved set or a portfolio."""
from fastapi import Depends
from fastapi.responses import Response

from . import db, export
from .app import app, require_auth


def _rows(portfolio_id: int | None):
    return db.portfolio_items(portfolio_id) if portfolio_id else db.list_saved()


@app.get("/export.csv")
def export_csv(portfolio_id: int | None = None, _=Depends(require_auth)):
    return Response(export.to_csv(_rows(portfolio_id)), media_type="text/csv",
                    headers={"content-disposition": 'attachment; filename="openlease.csv"'})


@app.get("/export.xlsx")
def export_xlsx(portfolio_id: int | None = None, _=Depends(require_auth)):
    return Response(
        export.to_xlsx(_rows(portfolio_id)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"content-disposition": 'attachment; filename="openlease.xlsx"'})
