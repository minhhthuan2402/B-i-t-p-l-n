from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from uuid import uuid4
from werkzeug.security import check_password_hash
from eapp import app, db, vnpay
from eapp.models import (User, RepairOrder, Regulation, Invoice, Payment,
                         PartCategory, Part, RegulationChangeHistory, VehicleIntake)


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


# Giu ten loi cu cho cac API thanh toan.
PaymentError = ApiError


def now():
    # Database luu thoi gian Viet Nam
    return datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)


def get_user_by_id(user_id):
    return db.session.get(User, user_id)


def authenticate(username, password):
    user = User.query.filter(User.username == username.strip(), User.status == 'ACTIVE').first()
    if user and check_password_hash(user.password_hash, password):
        return user

# DTO ROLE THU NGAN

def load_repair_orders(page=1):
    query = RepairOrder.query.filter(RepairOrder.status == 'COMPLETED',
                                    ~RepairOrder.invoice.has())
    return query.order_by(RepairOrder.id.desc()).offset((page - 1) * 20).limit(20).all()


def get_invoice(invoice_id, lock=False):
    query = Invoice.query.filter(Invoice.id == invoice_id)
    if lock:
        query = query.populate_existing().with_for_update()
    invoice = query.first()
    if not invoice:
        raise PaymentError('Khong tim thay hoa don.', 404)
    return invoice


def create_invoice(repair_order_id, cashier_id):
    order = RepairOrder.query.filter(RepairOrder.id == repair_order_id).with_for_update().first()
    if not order:
        raise PaymentError('Khong tim thay phieu sua chua.', 404)
    invoice = Invoice.query.filter(Invoice.repair_order_id == order.id).with_for_update().first()
    if invoice:
        return invoice, False
    if order.status != 'COMPLETED':
        raise PaymentError('Chi lap hoa don cho phieu da hoan thanh.', 409)
    if not order.items:
        raise PaymentError('Phieu sua chua chua co hang muc.', 409)
    regulation = get_current_regulation()

    labor = Decimal('0')
    parts = Decimal('0')
    for item in order.items:
        if item.price < 0 or item.quantity <= 0:
            raise PaymentError('Hang muc sua chua khong hop le.', 409)
        labor += item.price * item.quantity
        for usage in item.part_usages:
            if usage.price < 0 or usage.quantity <= 0:
                raise PaymentError('Chi tiet linh kien khong hop le.', 409)
            # So luong linh kien la tong so dung cho hang muc, khong nhan lai item.quantity.
            parts += usage.price * usage.quantity
    vat = ((labor + parts) * regulation.vat_rate / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    total = labor + parts + vat
    if total <= 0 or total > Decimal('9999999999999.99'):
        raise PaymentError('Tong tien hoa don khong hop le.', 409)
    invoice = Invoice(repair_order_id=order.id, cashier_id=cashier_id,
                      issued_at=now(), labor_cost=labor, parts_cost=parts,
                      vat_rate=regulation.vat_rate, vat_amount=vat,
                      total_amount=total, status='UNPAID')
    db.session.add(invoice)
    db.session.commit()
    return invoice, True


def load_invoices(status=None, page=1):
    query = Invoice.query
    if status:
        if status not in ('UNPAID', 'PAID', 'CANCELLED'):
            raise PaymentError('Trang thai hoa don khong hop le.')
        query = query.filter(Invoice.status == status)
    return query.order_by(Invoice.id.desc()).offset((page - 1) * 20).limit(20).all()


def create_payment(invoice_id, method, ip_address):
    invoice = get_invoice(invoice_id, lock=True)
    if invoice.status != 'UNPAID':
        raise PaymentError('Hoa don da thanh toan hoac da huy.', 409)
    if Payment.query.filter(Payment.invoice_id == invoice.id,
                            Payment.status.in_(('PENDING', 'SUCCESS'))).with_for_update().first():
        raise PaymentError('Hoa don co giao dich dang cho xac minh hoac da thanh cong.', 409)
    if method == 'VNPAY' and not vnpay.check_config():
        raise PaymentError('Chua cau hinh VNPAY.', 503)
    if method == 'VNPAY' and (invoice.total_amount != invoice.total_amount.to_integral_value()
                              or invoice.total_amount * 100 >= 10 ** 12):
        raise PaymentError('So tien VNPAY phai nguyen VND va nam trong gioi han 12 chu so sau khi nhan 100.')

    created_at = now()
    payment = Payment(invoice_id=invoice.id, amount=invoice.total_amount,
                      payment_method=method, transaction_reference=uuid4().hex,
                      created_at=created_at, status='PENDING')
    payment_url = None
    if method == 'CASH':
        payment.status = 'SUCCESS'
        payment.paid_at = created_at
        invoice.status = 'PAID'
    else:
        payment.expires_at = created_at + timedelta(minutes=15)
        payment_url = vnpay.create_payment_url(payment, ip_address)
    db.session.add(payment)
    db.session.commit()
    return payment, payment_url


def process_vnpay_ipn(data):
    if not vnpay.verify_signature(data) or data.get('vnp_TmnCode') != app.config['VNPAY_TMN_CODE']:
        return {'RspCode': '97', 'Message': 'Invalid signature or merchant'}
    reference = data.get('vnp_TxnRef')
    payment = Payment.query.filter(Payment.transaction_reference == reference,
                                   Payment.payment_method == 'VNPAY').first()
    if not payment:
        return {'RspCode': '01', 'Message': 'Order not found'}
    # Cung thu tu khoa voi API tao thanh toan: hoa don truoc, giao dich sau.
    invoice = get_invoice(payment.invoice_id, lock=True)
    payment = Payment.query.filter(Payment.id == payment.id).populate_existing().with_for_update().one()
    amount = data.get('vnp_Amount', '')
    if not amount.isascii() or not amount.isdigit() or len(amount) > 12 or int(amount) != int(payment.amount * 100):
        return {'RspCode': '04', 'Message': 'Invalid amount'}
    if payment.status != 'PENDING' or invoice.status != 'UNPAID':
        return {'RspCode': '02', 'Message': 'Order already confirmed'}
    response_code = data.get('vnp_ResponseCode', '')
    transaction_status = data.get('vnp_TransactionStatus', '')
    if len(response_code) != 2 or not response_code.isdigit() or len(transaction_status) != 2 or not transaction_status.isdigit():
        return {'RspCode': '99', 'Message': 'Invalid transaction result'}

    if response_code == '00' and transaction_status == '00':
        transaction_id = data.get('vnp_TransactionNo', '')
        if not transaction_id.isascii() or not transaction_id.isdigit() or len(transaction_id) > 15 or int(transaction_id) == 0:
            return {'RspCode': '99', 'Message': 'Invalid transaction number'}
        try:
            paid_at = datetime.strptime(data.get('vnp_PayDate', ''), '%Y%m%d%H%M%S')
        except ValueError:
            return {'RspCode': '99', 'Message': 'Invalid payment date'}
        payment.vnpay_transaction_id = transaction_id
        payment.paid_at = paid_at
        payment.status = 'SUCCESS'
        invoice.status = 'PAID'
    elif response_code in ('09', '10', '11', '12', '13', '24', '51', '65', '75', '79', '99') and transaction_status == '02':
        payment.status = 'CANCELLED' if response_code == '24' else 'FAILED'
    # Ket qua chua ro/bi nghi ngo giu PENDING de doi soat, khong thu tien lan hai.
    payment.response_code = response_code
    payment.bank_code = data.get('vnp_BankCode', '')[:30] or None
    db.session.commit()
    return {'RspCode': '00', 'Message': 'Confirm Success'}


def payment_to_dict(payment):
    return {
        'id': payment.id, 'invoice_id': payment.invoice_id,
        'transaction_reference': payment.transaction_reference,
        'payment_method': payment.payment_method, 'amount': str(payment.amount),
        'status': payment.status,
        'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
        'expires_at': payment.expires_at.isoformat() if payment.expires_at else None,
        'vnpay_transaction_id': payment.vnpay_transaction_id,
        'response_code': payment.response_code
    }


def invoice_to_dict(invoice):
    vehicle = invoice.repair_order.intake.vehicle
    return {
        'id': invoice.id, 'repair_order_id': invoice.repair_order_id,
        'cashier_id': invoice.cashier_id, 'status': invoice.status,
        'issued_at': invoice.issued_at.isoformat(), 'currency': 'VND',
        'customer_name': vehicle.customer.full_name, 'license_plate': vehicle.license_plate,
        'labor_cost': str(invoice.labor_cost), 'parts_cost': str(invoice.parts_cost),
        'vat_rate': str(invoice.vat_rate), 'vat_amount': str(invoice.vat_amount),
        'total_amount': str(invoice.total_amount),
        'items': [{'id': item.id, 'service_name': item.service_name,
                   'price': str(item.price), 'quantity': item.quantity,
                   'labor_amount': str(item.price * item.quantity),
                   'parts': [{'part_id': usage.part_id, 'name': usage.part.name,
                              'price': str(usage.price), 'quantity': usage.quantity,
                              'amount': str(usage.price * usage.quantity)}
                             for usage in item.part_usages]}
                  for item in invoice.repair_order.items],
        'payments': [payment_to_dict(p) for p in sorted(invoice.payments, key=lambda p: p.id)]
    }

# DTO ROLE ADMIN

def check_fields(data, allowed, required=()):
    if not data or set(data) - set(allowed):
        raise ApiError('Du lieu rong hoac co truong khong duoc ho tro.')
    if any(field not in data for field in required):
        raise ApiError('Thieu truong bat buoc: ' + ', '.join(required))


def text_value(value, field, limit, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value.strip()) > limit or (not optional and not value.strip()):
        raise ApiError(field + ' khong hop le.')
    return value.strip()


def integer_value(value, field, minimum=0):
    if type(value) is not int or not minimum <= value <= 2147483647:
        raise ApiError(field + ' phai la so nguyen tu ' + str(minimum) + ' den 2147483647.')
    return value


def decimal_value(value, field, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ApiError(field + ' phai la so.')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= Decimal(maximum):
            raise InvalidOperation
        rounded = result.quantize(Decimal('0.01'))
        if result != rounded:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ApiError(field + ' phai tu 0 den ' + maximum + ', toi da 2 chu so thap phan.')
    return rounded


def get_part_category(category_id, lock=False):
    query = PartCategory.query.filter(PartCategory.id == category_id)
    if lock:
        query = query.populate_existing().with_for_update()
    category = query.first()
    if not category:
        raise ApiError('Khong tim thay danh muc linh kien.', 404)
    return category


def load_part_categories(kw=None, status=None, page=1):
    query = PartCategory.query
    if kw:
        query = query.filter(PartCategory.name.contains(kw.strip()))
    if status is not None:
        if status not in ('ACTIVE', 'INACTIVE'):
            raise ApiError('status phai la ACTIVE hoac INACTIVE.')
        query = query.filter(PartCategory.status == status)
    return query.order_by(PartCategory.id).offset((page - 1) * 20).limit(20).all()


def save_part_category(data, category_id=None):
    check_fields(data, ('name', 'description', 'status'), ('name',) if category_id is None else ())
    values = {}
    if 'name' in data:
        values['name'] = text_value(data['name'], 'name', 120)
    if 'description' in data:
        values['description'] = text_value(data['description'], 'description', 255, optional=True)
    if 'status' in data:
        if data['status'] not in ('ACTIVE', 'INACTIVE'):
            raise ApiError('status phai la ACTIVE hoac INACTIVE.')
        values['status'] = data['status']
    category = get_part_category(category_id, lock=True) if category_id is not None else PartCategory()
    if 'name' in values:
        duplicate = PartCategory.query.filter(PartCategory.name == values['name'],
                                              PartCategory.id != category_id).first()
        if duplicate:
            raise ApiError('Ten danh muc da ton tai.', 409)
    if category_id is not None and values.get('status') == 'INACTIVE':
        if Part.query.filter(Part.category_id == category_id, Part.is_active.is_(True)).with_for_update().first():
            raise ApiError('Can ngung su dung cac linh kien trong danh muc truoc.', 409)
    for field, value in values.items():
        setattr(category, field, value)
    db.session.add(category)
    db.session.commit()
    return category


def get_part(part_id, lock=False):
    query = Part.query.filter(Part.id == part_id)
    if lock:
        query = query.populate_existing().with_for_update()
    part = query.first()
    if not part:
        raise ApiError('Khong tim thay linh kien.', 404)
    return part


def load_parts(kw=None, category_id=None, active=None, page=1):
    query = Part.query
    if kw:
        query = query.filter(Part.name.contains(kw.strip()))
    if category_id is not None:
        try:
            category_id = int(category_id)
        except ValueError:
            raise ApiError('category_id phai la so nguyen duong.')
        integer_value(category_id, 'category_id', 1)
        query = query.filter(Part.category_id == category_id)
    if active is not None:
        if active not in ('true', 'false'):
            raise ApiError('is_active phai la true hoac false.')
        query = query.filter(Part.is_active == (active == 'true'))
    return query.order_by(Part.id).offset((page - 1) * 20).limit(20).all()


def save_part(data, part_id=None):
    check_fields(data, ('category_id', 'name', 'price', 'stock_quantity', 'image', 'is_active'),
                 ('category_id', 'name', 'price') if part_id is None else ())
    values = {}
    if 'category_id' in data:
        values['category_id'] = integer_value(data['category_id'], 'category_id', 1)
    if 'name' in data:
        values['name'] = text_value(data['name'], 'name', 150)
    if 'price' in data:
        values['price'] = decimal_value(data['price'], 'price', '9999999999999.99')
    if 'stock_quantity' in data:
        values['stock_quantity'] = integer_value(data['stock_quantity'], 'stock_quantity')
    if 'image' in data:
        values['image'] = text_value(data['image'], 'image', 255, optional=True)
    if 'is_active' in data:
        if type(data['is_active']) is not bool:
            raise ApiError('is_active phai la boolean.')
        values['is_active'] = data['is_active']

    part = get_part(part_id) if part_id is not None else Part(is_active=True)
    # Khoa danh muc truoc linh kien, cung thu tu voi thao tac ngung danh muc.
    previous_category_id = part.category_id
    category_id = values.get('category_id', previous_category_id)
    category = get_part_category(category_id, lock=True)
    if part_id is not None:
        part = get_part(part_id, lock=True)
        if part.category_id != previous_category_id:
            raise ApiError('Linh kien vua duoc chuyen danh muc. Vui long thu lai.', 409)
    if values.get('is_active', part.is_active) and category.status != 'ACTIVE':
        raise ApiError('Khong the su dung linh kien trong danh muc da ngung.', 409)
    for field, value in values.items():
        setattr(part, field, value)
    db.session.add(part)
    db.session.commit()
    return part


def get_current_regulation(lock=False):
    query = Regulation.query.order_by(Regulation.updated_at.desc(), Regulation.id.desc())
    if lock:
        query = query.populate_existing().with_for_update()
    regulation = query.first()
    if not regulation:
        raise ApiError('Chua thiet lap quy dinh. Admin can khoi tao quy dinh.', 409)
    return regulation


def save_regulation(data, user_id, create=False):
    fields = ('vat_rate', 'max_vehicles_per_day')
    check_fields(data, fields, fields if create else ())
    values = {}
    if 'vat_rate' in data:
        values['vat_rate'] = decimal_value(data['vat_rate'], 'vat_rate', '100')
    if 'max_vehicles_per_day' in data:
        values['max_vehicles_per_day'] = integer_value(data['max_vehicles_per_day'], 'max_vehicles_per_day', 1)
    if create:
        if Regulation.query.with_for_update().first():
            raise ApiError('Quy dinh da ton tai. Dung PATCH de cap nhat.', 409)
        # ID co dinh khi khoi tao de rang buoc PK chan hai lan khoi tao dong thoi.
        regulation = Regulation(id=1, updated_at=now(), updated_by_id=user_id, **values)
        db.session.add(regulation)
    else:
        regulation = get_current_regulation(lock=True)
        changed_at = now()
        for field, value in values.items():
            old_value = getattr(regulation, field)
            if old_value != value:
                db.session.add(RegulationChangeHistory(
                    regulation_id=regulation.id, user_id=user_id,
                    field_name=field.upper(), old_value=str(old_value), new_value=str(value),
                    changed_at=changed_at))
                setattr(regulation, field, value)
                regulation.updated_by_id = user_id
                regulation.updated_at = changed_at
    db.session.commit()
    return regulation


def load_regulation_history(page=1):
    return RegulationChangeHistory.query.order_by(RegulationChangeHistory.changed_at.desc(),
                                                  RegulationChangeHistory.id.desc()).offset((page - 1) * 20).limit(20).all()


def get_intake_capacity(day=None):
    try:
        selected_day = date.fromisoformat(day) if day is not None else now().date()
    except ValueError:
        raise ApiError('date phai co dinh dang YYYY-MM-DD.')
    if day is not None and selected_day.isoformat() != day:
        raise ApiError('date phai co dinh dang YYYY-MM-DD.')
    if selected_day == date.max:
        raise ApiError('Ngay vuot qua pham vi ho tro.')
    regulation = get_current_regulation()
    start = datetime.combine(selected_day, time.min)
    count = VehicleIntake.query.filter(VehicleIntake.received_at >= start,
                                       VehicleIntake.received_at < start + timedelta(days=1),
                                       VehicleIntake.status != 'CANCELLED').count()
    return {'date': selected_day.isoformat(), 'max_vehicles_per_day': regulation.max_vehicles_per_day,
            'received_count': count, 'remaining': max(0, regulation.max_vehicles_per_day - count),
            'can_receive': count < regulation.max_vehicles_per_day}


def category_to_dict(category):
    return {'id': category.id, 'name': category.name, 'description': category.description,
            'status': category.status}


def part_to_dict(part):
    return {'id': part.id, 'category_id': part.category_id, 'name': part.name,
            'price': str(part.price), 'stock_quantity': part.stock_quantity,
            'image': part.image, 'is_active': part.is_active}


def regulation_to_dict(regulation):
    return {'id': regulation.id, 'vat_rate': str(regulation.vat_rate),
            'max_vehicles_per_day': regulation.max_vehicles_per_day,
            'updated_by_id': regulation.updated_by_id, 'updated_at': regulation.updated_at.isoformat()}
