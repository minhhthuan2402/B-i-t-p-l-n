
from flask import redirect, render_template, request, jsonify, session
from flask_login import current_user, login_user, logout_user
from functools import wraps
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from eapp import app, db, dao, login, vnpay
import secrets
import hmac


@login.user_loader
def get_user(user_id):
    return dao.get_user_by_id(int(user_id))

# thiết lập phân quyền
def roles_required(*roles):
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify(error='Can dang nhap.'), 401
            if current_user.status != 'ACTIVE' or current_user.role not in roles:
                return jsonify(error='Khong co quyen su dung chuc nang nay.'), 403
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                expected = session.get('csrf_token', '')
                if not expected or not hmac.compare_digest(expected.encode('utf-8'), request.headers.get('X-CSRF-Token', '').encode('utf-8')):
                    return jsonify(error='CSRF token khong hop le.'), 403
            return function(*args, **kwargs)
        return wrapper
    return decorator


cashier_required = roles_required('CASHIER')
admin_required = roles_required('ADMIN')
receptionist_required = roles_required('RECEPTIONIST')
TECHNICIAN_required = roles_required('TECHNICIAN')


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
    if request.path.startswith('/api/admin/') and isinstance(error, IntegrityError):
        return jsonify(error='Du lieu trung lap hoac dang duoc tham chieu. Vui long kiem tra lai.'), 409
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
    if user.role not in ('CASHIER', 'ADMIN'):
        return jsonify(error='Tai khoan khong co quyen su dung API thanh toan/quan ly quy dinh.'), 403
    session.clear()
    login_user(user)
    session['csrf_token'] = secrets.token_hex(32)
    return jsonify(user={'id': user.id, 'full_name': user.full_name, 'role': user.role},
                   csrf_token=session['csrf_token'])


@app.route('/api/auth/logout', methods=['POST'])
@roles_required('CASHIER', 'ADMIN')
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


# quản lý linh kiện cho Admin.
@app.route('/api/admin/part-categories', methods=['GET', 'POST'])
@admin_required
def admin_part_categories():
    if request.method == 'POST':
        category = dao.save_part_category(json_body())
        return jsonify(data=dao.category_to_dict(category)), 201
    categories = dao.load_part_categories(kw=request.args.get('kw'),
                                          status=request.args.get('status'), page=get_page())
    return jsonify(data=[dao.category_to_dict(c) for c in categories])


@app.route('/api/admin/part-categories/<int:category_id>', methods=['GET', 'PATCH', 'DELETE'])
@admin_required
def admin_part_category(category_id):
    if request.method == 'PATCH':
        category = dao.save_part_category(json_body(), category_id)
    elif request.method == 'DELETE':
        category = dao.save_part_category({'status': 'INACTIVE'}, category_id)
    else:
        category = dao.get_part_category(category_id)
    return jsonify(data=dao.category_to_dict(category))


@app.route('/api/admin/parts', methods=['GET', 'POST'])
@admin_required
def admin_parts():
    if request.method == 'POST':
        part = dao.save_part(json_body())
        return jsonify(data=dao.part_to_dict(part)), 201
    parts = dao.load_parts(kw=request.args.get('kw'), category_id=request.args.get('category_id'),
                           active=request.args.get('is_active'), page=get_page())
    return jsonify(data=[dao.part_to_dict(p) for p in parts])


@app.route('/api/admin/parts/<int:part_id>', methods=['GET', 'PATCH', 'DELETE'])
@admin_required
def admin_part(part_id):
    if request.method == 'PATCH':
        part = dao.save_part(json_body(), part_id)
    elif request.method == 'DELETE':
        part = dao.save_part({'is_active': False}, part_id)
    else:
        part = dao.get_part(part_id)
    return jsonify(data=dao.part_to_dict(part))

#quản lý quy định cho Admin.
@app.route('/api/admin/regulations', methods=['GET', 'POST', 'PATCH'])
@admin_required
def admin_regulations():
    if request.method == 'GET':
        regulation = dao.get_current_regulation()
    else:
        regulation = dao.save_regulation(json_body(), current_user.id, create=request.method == 'POST')
    return jsonify(data=dao.regulation_to_dict(regulation)), 201 if request.method == 'POST' else 200


@app.route('/api/admin/regulations/history')
@admin_required
def admin_regulation_history():
    history = dao.load_regulation_history(page=get_page())
    return jsonify(data=[{'id': h.id, 'regulation_id': h.regulation_id,
                          'user_id': h.user_id, 'field_name': h.field_name,
                          'old_value': h.old_value, 'new_value': h.new_value,
                          'changed_at': h.changed_at.isoformat()} for h in history])


@app.route('/api/admin/regulations/intake-capacity')
@admin_required
def admin_intake_capacity():
    return jsonify(data=dao.get_intake_capacity(request.args.get('date')))


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
