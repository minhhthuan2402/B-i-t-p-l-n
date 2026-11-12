
from flask import Flask, render_template, request
from unicodedata import category

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')
@app.route('/login')
def login_view():
    return render_template('login.html')

@app.route('/register')
def register_view():
    return render_template('register.html')

if __name__ == '__main__':
    with app.app_context():
        app.run(debug=True)