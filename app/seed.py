"""Demo listings — three per metro. Lets search/map/rank be exercised (and tested)
before the crawler lands, and gives the README a keyless demo that touches no broker
site. `python -m app.seed` to (re)load."""
from .db import get_conn, init_db, save_listing

DEMO = [
    dict(metro="nyc", source="seed", source_url="seed://nyc/1", address="55 Gansevoort St, New York, NY",
         neighborhood="Meatpacking District", borough="Manhattan", lat=40.7392, lng=-74.0072,
         property_type="retail", transaction_type="lease", size_sf=2400, asking_rent=325, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=14.0, broker_firm="Demo Realty",
         our_description="Corner retail with 40 feet of frontage on a heavily trafficked cobblestone block."),
    dict(metro="nyc", source="seed", source_url="seed://nyc/2", address="1412 Broadway, New York, NY",
         neighborhood="Garment District", borough="Manhattan", lat=40.7538, lng=-73.9876,
         property_type="office", transaction_type="lease", size_sf=8200, asking_rent=62, rent_unit="sf_yr",
         lease_type="modified gross", floor="14", broker_firm="Demo Realty",
         our_description="Full-floor pre-built office one block from Bryant Park, wired and ready."),
    dict(metro="nyc", source="seed", source_url="seed://nyc/3", address="35-10 Astoria Blvd, Queens, NY",
         neighborhood="Astoria", borough="Queens", lat=40.7719, lng=-73.9196,
         property_type="industrial", transaction_type="lease", size_sf=15000, asking_rent=34, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=22.0, broker_firm="Demo Realty",
         our_description="Clear-span warehouse with drive-in loading, minutes from the Grand Central Parkway."),
    dict(metro="mia", source="seed", source_url="seed://mia/1", address="2618 NW 2nd Ave, Miami, FL",
         neighborhood="Wynwood", borough="Miami", lat=25.8015, lng=-80.1993,
         property_type="retail", transaction_type="lease", size_sf=1500, asking_rent=95, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Ground-floor retail in the middle of the Wynwood walls foot traffic."),
    dict(metro="mia", source="seed", source_url="seed://mia/2", address="1200 Brickell Ave, Miami, FL",
         neighborhood="Brickell", borough="Miami", lat=25.7601, lng=-80.1918,
         property_type="office", transaction_type="lease", size_sf=5400, asking_rent=78, rent_unit="sf_yr",
         lease_type="modified gross", floor="9", broker_firm="Demo Realty",
         our_description="Bay-view office suite in the core of Brickell's financial corridor."),
    dict(metro="mia", source="seed", source_url="seed://mia/3", address="7800 NW 25th St, Doral, FL",
         neighborhood="Doral", borough="Doral", lat=25.8000, lng=-80.3300,
         property_type="industrial", transaction_type="lease", size_sf=22000, asking_rent=16, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=28.0, broker_firm="Demo Realty",
         our_description="Airport-adjacent distribution space with dock-high loading."),
    dict(metro="la", source="seed", source_url="seed://la/1", address="8000 Melrose Ave, Los Angeles, CA",
         neighborhood="Melrose", borough="Hollywood", lat=34.0836, lng=-118.3639,
         property_type="retail", transaction_type="lease", size_sf=1800, asking_rent=72, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Boutique storefront on the Melrose shopping run, big glass line."),
    dict(metro="la", source="seed", source_url="seed://la/2", address="1100 S Flower St, Los Angeles, CA",
         neighborhood="South Park", borough="Downtown", lat=34.0407, lng=-118.2650,
         property_type="office", transaction_type="lease", size_sf=6100, asking_rent=44, rent_unit="sf_yr",
         lease_type="modified gross", floor="6", broker_firm="Demo Realty",
         our_description="Creative office loft with exposed brick, walking distance to the arena."),
    dict(metro="la", source="seed", source_url="seed://la/3", address="2500 E Vernon Ave, Vernon, CA",
         neighborhood="Vernon", borough="South Bay", lat=34.0033, lng=-118.2100,
         property_type="industrial", transaction_type="lease", size_sf=48000, asking_rent=19, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=26.0, broker_firm="Demo Realty",
         our_description="Heavy-power manufacturing box in the Vernon industrial belt."),
    dict(metro="chi", source="seed", source_url="seed://chi/1", address="1550 N Damen Ave, Chicago, IL",
         neighborhood="Wicker Park", borough="Wicker Park", lat=41.9101, lng=-87.6773,
         property_type="retail", transaction_type="lease", size_sf=2100, asking_rent=58, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Corner retail at the six-way, the busiest pedestrian node in Wicker Park."),
    dict(metro="chi", source="seed", source_url="seed://chi/2", address="222 W Merchandise Mart Plaza, Chicago, IL",
         neighborhood="River North", borough="River North", lat=41.8885, lng=-87.6354,
         property_type="office", transaction_type="lease", size_sf=12000, asking_rent=41, rent_unit="sf_yr",
         lease_type="gross", floor="12", broker_firm="Demo Realty",
         our_description="Tech-tenant floor in the Mart with river views and its own L stop."),
    dict(metro="chi", source="seed", source_url="seed://chi/3", address="4400 S Kildare Ave, Chicago, IL",
         neighborhood="Archer Heights", borough="Archer Heights", lat=41.8130, lng=-87.7310,
         property_type="industrial", transaction_type="sale", size_sf=31000, sale_price=2950000,
         ceiling_height_ft=24.0, broker_firm="Demo Realty",
         our_description="Owner-user industrial building for sale with rail-adjacent yard."),
]


def seed() -> int:
    init_db()
    for rec in DEMO:
        save_listing(rec)
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM listing").fetchone()["c"]


if __name__ == "__main__":
    print(f"seeded — {seed()} listings")
