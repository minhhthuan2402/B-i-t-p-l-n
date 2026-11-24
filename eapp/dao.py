from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from werkzeug.security import check_password_hash
from eapp import app, db, vnpay
from eapp.models import User, RepairOrder, Regulation, Invoice, Payment


class PaymentError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def now():
    # Database luu thoi gian Viet Nam
    return datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)


def get_user_by_id(user_id):
    return db.session.get(User, user_id)


def authenticate(username, password):
    user = User.query.filter(User.username == username.strip(), User.status == 'ACTIVE').first()
    if user and check_password_hash(user.password_hash, password):
        return user


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
    regulation = Regulation.query.order_by(Regulation.updated_at.desc(), Regulation.id.desc()).first()
    if not regulation:
        raise PaymentError('Chua thiet lap quy dinh VAT.', 409)

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
