"""CSV / XLSX export of a result set. stdlib csv for CSV; openpyxl for XLSX. The column
set is what a tenant rep actually sends a client — and note what is NOT exportable:
the broker's prose (we never stored it) and their photos (we never downloaded them)."""
import csv
import io

FIELDS = [
    "address", "neighborhood", "borough", "metro", "property_type", "transaction_type",
    "size_sf", "divisible_min_sf", "divisible_max_sf", "floor", "ceiling_height_ft",
    "asking_rent", "rent_unit", "lease_type", "sale_price", "availability_date",
    "walk_score", "transit_score", "broker_name", "broker_firm", "broker_phone",
    "our_description", "source", "source_url",
]


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def to_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "OpenLease export"
    ws.append(FIELDS)
    for r in rows:
        ws.append([r.get(f) for f in FIELDS])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
