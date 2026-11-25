from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, Boolean,
    ForeignKey, Enum, CheckConstraint, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from eapp import db, app
from flask_login import UserMixin


class BaseModel(db.Model):
    __abstract__ = True

    id = Column(Integer, primary_key=True, autoincrement=True)


class User(BaseModel, UserMixin):
    full_name = Column(String(120), nullable=False)
    username = Column(String(60), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum("RECEPTIONIST", "TECHNICIAN", "CASHIER", "ADMIN", name="user_role"), nullable=False)
    status = Column(Enum("ACTIVE", "LOCKED", name="user_status"), nullable=False, default="ACTIVE")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __str__(self):
        return self.name


class Customer(BaseModel):
    full_name = Column(String(120), nullable=False)
    phone_number = Column(String(20), index=True)
    address = Column(String(255))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __str__(self):
        return self.name


class Vehicle(BaseModel):
    customer_id = Column(Integer, ForeignKey(Customer.id, ondelete="RESTRICT"), nullable=False)
    license_plate = Column(String(20), nullable=False, unique=True)
    vehicle_type = Column(Enum("CAR", "MOTORBIKE", name="vehicle_type"), nullable=False, index=True)
    brand = Column(String(80))

    customer = relationship("Customer", backref="vehicles")

    def __str__(self):
        return self.name


class Regulation(BaseModel):
    vat_rate = Column(Numeric(5, 2), nullable=False, default=10)
    max_vehicles_per_day = Column(Integer, nullable=False, default=30)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_id = Column(Integer, ForeignKey(User.id , ondelete="SET NULL"))

    updated_by = relationship("User", foreign_keys=[updated_by_id])

    __table_args__ = (
        CheckConstraint("vat_rate >= 0 AND vat_rate <= 100", name="ck_regulations_vat_rate"),
        CheckConstraint("max_vehicles_per_day > 0", name="ck_regulations_max_vehicles"),
    )

    def __str__(self):
        return self.name


class RegulationChangeHistory(BaseModel):
    regulation_id = Column(Integer, ForeignKey(Regulation.id, ondelete="RESTRICT"), nullable=False)
    user_id = Column(Integer, ForeignKey( User.id, ondelete="SET NULL"))
    field_name = Column(Enum("VAT_RATE", "MAX_VEHICLES_PER_DAY", name="regulation_field"), nullable=False)
    old_value = Column(String(50),nullable=False)
    new_value = Column(String(50),nullable=False)
    changed_at = Column(DateTime, server_default=func.now(), nullable=False)

    regulation = relationship("Regulation", backref="change_history")
    user = relationship("User")

    def __str__(self):
        return self.name


class VehicleIntake(BaseModel):
    vehicle_id = Column(Integer, ForeignKey(Vehicle.id, ondelete="RESTRICT"), nullable=False, index=True)
    receptionist_id = Column(Integer, ForeignKey(User.id, ondelete="RESTRICT"), nullable=False)
    received_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    description = Column(Text, nullable=False)
    status = Column(
        Enum(
            "RECEIVED",
            "IN_REPAIR",
            "REPAIRED",
            "DELIVERED",
            "CANCELLED",
            name="intake_status"
        ),
        nullable=False,
        default="RECEIVED"
    )

    vehicle = relationship("Vehicle", backref="intakes")
    receptionist = relationship("User", foreign_keys=[receptionist_id])

    def __str__(self):
        return self.name


class RepairOrder(BaseModel):
    intake_id = Column(Integer, ForeignKey(VehicleIntake.id, ondelete="RESTRICT"), nullable=False, unique=True)
    technician_id = Column(Integer, ForeignKey(User.id, ondelete="RESTRICT"),nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    total_cost = Column(Numeric(15, 2), nullable=False, default=0)
    status = Column(
        Enum(
            "IN_PROGRESS",
            "COMPLETED",
            "CANCELLED",
            name="repair_order_status"
        ),
        nullable=False,
        default="IN_PROGRESS"
    )

    intake = relationship("VehicleIntake", backref=db.backref("repair_order", uselist=False))
    technician = relationship("User", foreign_keys=[technician_id])

    __table_args__ = (
        CheckConstraint("total_cost >= 0", name="ck_repair_orders_total_cost"),
    )

    def __str__(self):
        return self.name


class RepairOrderItem(BaseModel):
    repair_order_id = Column(Integer, ForeignKey(RepairOrder.id, ondelete="RESTRICT"), nullable=False, index=True)
    service_name = Column(String(200), nullable=False)
    price = Column(Numeric(15, 2), nullable=False, default=0)
    quantity = Column(Integer, nullable=False, default=1)
    notes = Column(String(255))

    repair_order = relationship("RepairOrder",backref="items")

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_repair_order_items_price"),
        CheckConstraint("quantity > 0", name="ck_repair_order_items_quantity"),
    )

    def __str__(self):
        return self.name


class PartCategory(BaseModel):
    name = Column(String(120), nullable=False, unique=True)
    description = Column(String(255))
    status = Column(Enum("ACTIVE", "INACTIVE", name="part_category_status"), nullable=False, default="ACTIVE")

    def __str__(self):
        return self.name


class Part(BaseModel):
    category_id = Column(Integer, ForeignKey(PartCategory.id, ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(150), nullable=False, index=True)
    price = Column(Numeric(15, 2), nullable=False)
    stock_quantity = Column( Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    # lưu URL hình minh họa
    image = Column(String(255),nullable=True)
    category = relationship("PartCategory", backref="parts")

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_parts_price"),
        CheckConstraint("stock_quantity >= 0",name="ck_parts_stock_quantity"),
    )

    def __str__(self):
        return self.name


class RepairPartUsage(BaseModel):
    repair_order_item_id = Column(Integer, ForeignKey(RepairOrderItem.id, ondelete="RESTRICT"), nullable=False)
    part_id = Column(Integer, ForeignKey(Part.id, ondelete="RESTRICT"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    price = Column( Numeric(15, 2), nullable=False)

    repair_order_item = relationship("RepairOrderItem", backref="part_usages")
    part = relationship("Part", backref="usages")

    __table_args__ = (
        UniqueConstraint("repair_order_item_id", "part_id", name="uq_repair_part_usage_item_part"),
        CheckConstraint("quantity > 0", name="ck_repair_part_usages_quantity"),
        CheckConstraint("price >= 0", name="ck_repair_part_usages_price"),
    )

    def __str__(self):
        return self.name


class Invoice(BaseModel):
    repair_order_id = Column(Integer, ForeignKey(RepairOrder.id, ondelete="RESTRICT"), nullable=False, unique=True)
    cashier_id = Column(Integer, ForeignKey(User.id, ondelete="RESTRICT"), nullable=False)
    issued_at = Column( DateTime, server_default=func.now(), nullable=False, index=True)
    labor_cost = Column(Numeric(15, 2), nullable=False, default=0)
    parts_cost = Column(Numeric(15, 2), nullable=False, default=0)
    vat_rate = Column(Numeric(5, 2), nullable=False)
    vat_amount = Column(Numeric(15, 2), nullable=False, default=0)
    total_amount = Column(Numeric(15, 2), nullable=False, default=0)
    status = Column(
        Enum(
            "UNPAID",
            "PAID",
            "CANCELLED",
            name="invoice_status"
        ),
        nullable=False,
        default="UNPAID",
        index=True
    )

    repair_order = relationship("RepairOrder", backref=db.backref("invoice", uselist=False))
    cashier = relationship("User", foreign_keys=[cashier_id])

    __table_args__ = (
        CheckConstraint(
            "labor_cost >= 0 AND "
            "parts_cost >= 0 AND "
            "vat_amount >= 0 AND "
            "total_amount >= 0",
            name="ck_invoices_amounts"
        ),
        CheckConstraint("vat_rate >= 0 AND vat_rate <= 100", name="ck_invoices_vat_rate"),
    )

    def __str__(self):
        return self.name


class Payment(BaseModel):
    invoice_id = Column(Integer, ForeignKey(Invoice.id, ondelete="RESTRICT"), nullable=False, index=True)
    paid_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(),nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    payment_method = Column(Enum("CASH", "VNPAY", name="payment_method"), nullable=False)
    transaction_reference = Column(String(100), nullable=False, unique=True)
    vnpay_transaction_id = Column(String(100), unique=True)
    bank_code = Column(String(30))
    response_code = Column(String(20))
    expires_at = Column(DateTime)
    status = Column(Enum("PENDING", "SUCCESS", "FAILED", "CANCELLED", "EXPIRED", name="payment_status"), nullable=False, default="PENDING", index=True)

    invoice = relationship("Invoice", backref="payments")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount"),
    )

    def __str__(self):
        return self.name

if __name__ == "__main__":
    with app.app_context():
        db.create_all()