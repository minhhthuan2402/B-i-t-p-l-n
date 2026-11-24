
from flask import redirect, render_template, request, jsonify, session
from flask_login import current_user, login_user, logout_user
from functools import wraps
from sqlalchemy.exc import SQLAlchemyError
from eapp import app, db, dao, login, vnpay
import secrets
import hmac


@login.user_loader
def get_user(user_id):
    return dao.get_user_by_id(int(user_id))


def cashier_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify(error='Can dang nhap.'), 401
        if current_user.status != 'ACTIVE' or current_user.role != 'CASHIER':
            return jsonify(error='Chi thu ngan duoc su dung API thanh toan.'), 403
        if request.method == 'POST':
            expected = session.get('csrf_token', '')
            if not expected or not hmac.compare_digest(expected.encode('utf-8'), request.headers.get('X-CSRF-Token', '').encode('utf-8')):
                return jsonify(error='CSRF token khong hop le.'), 403
        return function(*args, **kwargs)
    return wrapper


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise dao.PaymentError('Can gui JSON object.')
    return data


def get_page():
    try:
        page = int(request.args.get('page', '1'))
    except ValueError:
        raise dao.PaymentError('page phai la so nguyen duong.')
    if page < 1:
        raise dao.PaymentError('page phai la so nguyen duong.')
    return page


@app.errorhandler(dao.PaymentError)
def payment_error(error):
    db.session.rollback()
    return jsonify(error=error.message), error.status


@app.errorhandler(SQLAlchemyError)
def database_error(error):
    db.session.rollback()
    app.logger.exception('Database operation failed')
    if request.path == '/api/payments/vnpay/ipn':
        return jsonify(RspCode='99', Message='Database error')
    return jsonify(error='Khong the luu/lay du lieu. Vui long thu lai.'), 500


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = json_body()
    username = data.get('username')
    password = data.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        raise dao.PaymentError('Can username va password.')
    user = dao.authenticate(username, password)
    if not user:
        return jsonify(error='Sai tai khoan, mat khau hoac tai khoan bi khoa.'), 401
    if user.role != 'CASHIER':
        return jsonify(error='Chi thu ngan duoc dang nhap API thanh toan.'), 403
    session.clear()
    login_user(user)
    session['csrf_token'] = secrets.token_hex(32)
    return jsonify(user={'id': user.id, 'full_name': user.full_name, 'role': user.role},
                   csrf_token=session['csrf_token'])


@app.route('/api/auth/logout', methods=['POST'])
@cashier_required
def api_logout():
    logout_user()
    session.clear()
    return jsonify(message='Da dang xuat.')


@app.route('/api/repair-orders/payable')
@cashier_required
def payable_repair_orders():
    orders = dao.load_repair_orders(page=get_page())
    return jsonify(data=[{'id': order.id, 'status': order.status,
                          'license_plate': order.intake.vehicle.license_plate,
                          'customer_name': order.intake.vehicle.customer.full_name}
                         for order in orders])

#Tạo hóa đơn từ phiếu sửa chữa
@app.route('/api/invoices', methods=['POST'])
@cashier_required
def create_invoice():
    repair_order_id = json_body().get('repair_order_id')
    if type(repair_order_id) is not int or repair_order_id <= 0:
        raise dao.PaymentError('repair_order_id phai la so nguyen duong.')
    invoice, created = dao.create_invoice(repair_order_id, current_user.id)
    return jsonify(data=dao.invoice_to_dict(invoice)), 201 if created else 200

#Lấy danh sách hóa đơn
@app.route('/api/invoices')
@cashier_required
def load_invoices():
    invoices = dao.load_invoices(status=request.args.get('status'), page=get_page())
    return jsonify(data=[dao.invoice_to_dict(invoice) for invoice in invoices])

#Xem chi tiết hóa đơn
@app.route('/api/invoices/<int:invoice_id>')
@cashier_required
def invoice_detail(invoice_id):
    return jsonify(data=dao.invoice_to_dict(dao.get_invoice(invoice_id)))

#Tạo giao dịch thanh toán cho hóa đơn
@app.route('/api/invoices/<int:invoice_id>/payments', methods=['POST'])
@cashier_required
def pay_invoice(invoice_id):
    method = json_body().get('payment_method')
    if method not in ('CASH', 'VNPAY'):
        raise dao.PaymentError('payment_method phai la CASH hoac VNPAY.')
    payment, payment_url = dao.create_payment(invoice_id, method, request.remote_addr or '127.0.0.1')
    return jsonify(data=dao.payment_to_dict(payment), payment_url=payment_url), 201

#Xem biên lai thanh toán
@app.route('/api/invoices/<int:invoice_id>/receipt')
@cashier_required
def invoice_receipt(invoice_id):
    invoice = dao.get_invoice(invoice_id)
    if invoice.status != 'PAID':
        raise dao.PaymentError('Chi xuat hoa don da thanh toan.', 409)
    return jsonify(data=dao.invoice_to_dict(invoice))

#Xử lý IPN từ VNPAY
@app.route('/api/payments/vnpay/ipn')
def vnpay_ipn():
    if any(len(request.args.getlist(key)) != 1 for key in request.args):
        return jsonify(RspCode='97', Message='Duplicate parameter')
    return jsonify(dao.process_vnpay_ipn(request.args.to_dict()))

#Xử lý Return URL từ VNPAY
@app.route('/api/payments/vnpay/return')
def vnpay_return():
    data = request.args.to_dict()
    if any(len(request.args.getlist(key)) != 1 for key in request.args) or not vnpay.verify_signature(data) or data.get('vnp_TmnCode') != app.config['VNPAY_TMN_CODE']:
        return jsonify(error='Chu ky VNPAY khong hop le.'), 400
    # Return URL chi hien thi ket qua, IPN moi cap nhat database.
    return jsonify(transaction_reference=data.get('vnp_TxnRef'),
                   response_code=data.get('vnp_ResponseCode'),
                   message='Kiem tra trang thai hoa don qua API; Return URL khong xac nhan da thu tien.')


@app.route('/')
def index():
    return render_template('index.html')
@app.route('/login')
def login_view():
    return render_template('login.html')

@app.route('/register')
def register_view():
    return render_template('register.html')

@app.route('/repair-intake')
def repair_intake():
    return render_template('repair-intake.html')

@app.route('/create-ticket')
def create_ticket():
    return render_template('create-ticket.html')

@app.route('/reports')
def reports():
    return render_template('reports.html')

@app.route('/regulations')
def regulations():
    return render_template('regulations.html')

if __name__ == '__main__':
    with app.app_context():
        app.run(debug=True)
