"""Quick demo seed – admin, farmer, vendor, sample lots/orders, one engine run."""
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, SQLModel, select

from app.database import engine
from app.models import User, SupplyLot, DemandOrder
from app.services.auth import hash_password
from app.services.engine_service import run_engine_service


def seed():
    with Session(engine) as db:
        # Ensure all tables exist before we try to query or insert
        SQLModel.metadata.create_all(engine)

        # Skip if already seeded
        if db.exec(select(User)).first():
            print("Database already seeded.")
            return

        # Admin
        admin = User(
            name="Admin User",
            phone="0300000000",          # ← 10‑digit local number
            password_hash=hash_password("admin123"),
            role="admin",
            town="Kumasi",
            region="Ashanti",
            district="Kumasi Metropolitan",
            is_active=True,
        )
        db.add(admin)

        # Farmer (Kwadwo Mensah – Ejisu)
        farmer = User(
            name="Kwadwo Mensah",
            phone="0241234567",          # ← 10‑digit local number
            password_hash=hash_password("farmer123"),
            role="farmer",
            town="Ejisu",
            region="Ashanti",
            district="Ejisu Municipal",
            is_active=True,
        )
        db.add(farmer)

        # Vendor (Bola Market – Kumasi)
        vendor = User(
            name="Bola Market",
            phone="0509876543",          # ← 10‑digit local number
            password_hash=hash_password("vendor123"),
            role="vendor",
            town="Kumasi",
            region="Ashanti",
            district="Kumasi Metropolitan",
            is_active=True,
        )
        db.add(vendor)
        db.commit()
        db.refresh(farmer)
        db.refresh(vendor)

        # Sample supply lots (Ejisu area)
        now = datetime.now(timezone.utc)
        lots = [
            SupplyLot(
                farmer_id=farmer.id,
                crop_type="tomato",
                quantity_kg=50,
                spoilage_time=now + timedelta(hours=10),
                lat=6.7170,
                lon=-1.4690,
                location_label="Ejisu, Ejisu Municipal, Ashanti",
            ),
            SupplyLot(
                farmer_id=farmer.id,
                crop_type="tomato",
                quantity_kg=30,
                spoilage_time=now + timedelta(hours=20),
                lat=6.69,
                lon=-1.63,
                location_label="Ejisu, Ejisu Municipal, Ashanti",
            ),
            SupplyLot(
                farmer_id=farmer.id,
                crop_type="onion",
                quantity_kg=40,
                spoilage_time=now + timedelta(hours=15),
                lat=6.67,
                lon=-1.61,
                location_label="Ejisu, Ejisu Municipal, Ashanti",
            ),
        ]
        for lot in lots:
            db.add(lot)

        # Sample demand orders (Kumasi area)
        orders = [
            DemandOrder(
                vendor_id=vendor.id,
                crop_type="tomato",
                quantity_kg=60,
                min_shelf_life_h=6,
                lat=6.6885,
                lon=-1.6244,
                location_label="Kumasi, Kumasi Metropolitan, Ashanti",
            ),
            DemandOrder(
                vendor_id=vendor.id,
                crop_type="onion",
                quantity_kg=30,
                min_shelf_life_h=8,
                lat=6.6885,
                lon=-1.6244,
                location_label="Kumasi, Kumasi Metropolitan, Ashanti",
            ),
        ]
        for order in orders:
            db.add(order)
        db.commit()

        # Run engine once
        print("Running engine with demo data...")
        summary, _ = run_engine_service(db)
        print("Engine summary:", summary)


if __name__ == "__main__":
    seed()