import hashlib
import hmac
from urllib.parse import urlencode
from eapp import app


def check_config():
    return all(app.config.get(key) for key in (
        'VNPAY_TMN_CODE', 'VNPAY_HASH_SECRET', 'VNPAY_RETURN_URL'))


def sign_data(data):
    query = urlencode(sorted((k, str(v)) for k, v in data.items()
                             if k.startswith('vnp_') and v != ''
                             and k not in ('vnp_SecureHash', 'vnp_SecureHashType')))
    signature = hmac.new(app.config['VNPAY_HASH_SECRET'].encode('utf-8'),
                         query.encode('utf-8'), hashlib.sha512).hexdigest()
    return query, signature


def verify_signature(data):
    if not app.config['VNPAY_HASH_SECRET']:
        return False
    return hmac.compare_digest(sign_data(data)[1].encode('utf-8'),
                               data.get('vnp_SecureHash', '').encode('utf-8'))


def create_payment_url(payment, ip_address):
    data = {
        'vnp_Version': '2.1.0', 
        'vnp_Command': 'pay',
        'vnp_TmnCode': app.config['VNPAY_TMN_CODE'],
        'vnp_Amount': str(int(payment.amount * 100)),
        'vnp_CurrCode': 'VND', 
        'vnp_TxnRef': payment.transaction_reference,
        'vnp_OrderInfo': 'Thanh toan hoa don ' + str(payment.invoice_id),
        'vnp_OrderType': 'other', 
        'vnp_Locale': 'vn',
        # 'vnp_BankCode': 'VNPAYQR',
        'vnp_ReturnUrl': app.config['VNPAY_RETURN_URL'],
        'vnp_IpAddr': ip_address,
        'vnp_CreateDate': payment.created_at.strftime('%Y%m%d%H%M%S'),
        'vnp_ExpireDate': payment.expires_at.strftime('%Y%m%d%H%M%S')
    }
    query, signature = sign_data(data)
    return app.config['VNPAY_PAYMENT_URL'] + '?' + query + '&vnp_SecureHash=' + signature