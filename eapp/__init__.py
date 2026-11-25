import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from flask_login import LoginManager
import secrets

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['VNPAY_TMN_CODE'] = os.getenv('VNPAY_TMN_CODE', '')
app.config['VNPAY_HASH_SECRET'] = os.getenv('VNPAY_HASH_SECRET', '')
app.config['VNPAY_PAYMENT_URL'] = os.getenv('VNPAY_PAYMENT_URL', 'https://sandbox.vnpayment.vn/paymentv2/vpcpay.html')
app.config['VNPAY_RETURN_URL'] = os.getenv('VNPAY_RETURN_URL', '')
app.config["SQLALCHEMY_DATABASE_URI"] =os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = True

db = SQLAlchemy(app = app)
login = LoginManager(app=app)
