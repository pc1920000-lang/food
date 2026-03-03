import os
import sqlite3
from flask import Flask, g, render_template, request, redirect, url_for, session, jsonify
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
# prefer eventlet when available, otherwise fall back to threading to avoid
# "Invalid async_mode specified" errors when eventlet isn't installed
try:
    import eventlet  # noqa: F401
    _async_mode = 'eventlet'
except Exception:
    _async_mode = 'threading'
socketio = SocketIO(app, async_mode=_async_mode)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
RESTAURANT_NAME = os.environ.get('RESTAURANT_NAME', 'My Restaurant')

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    cur = db.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        available INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_number TEXT,
        status TEXT,
        total REAL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        item_id INTEGER,
        name TEXT,
        qty INTEGER,
        price REAL
    );
    ''')
    db.commit()
    # ensure image column exists for items (migrate if needed)
    cur = db.execute("PRAGMA table_info(items)")
    cols = [r['name'] for r in cur.fetchall()]
    if 'image' not in cols:
        db.execute('ALTER TABLE items ADD COLUMN image TEXT')
        db.commit()
    # ensure customer fields exist on orders
    if 'customer_name' not in cols:
        # PRAGMA table_info returned items columns; check orders separately
        cur2 = db.execute("PRAGMA table_info(orders)")
        order_cols = [r['name'] for r in cur2.fetchall()]
        if 'customer_name' not in order_cols:
            db.execute('ALTER TABLE orders ADD COLUMN customer_name TEXT')
        if 'customer_phone' not in order_cols:
            db.execute('ALTER TABLE orders ADD COLUMN customer_phone TEXT')
        db.commit()

# In Flask 3 `before_first_request` was removed; initialize DB at startup
def setup():
    # ensure upload folder exists and initialize DB within app context
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    with app.app_context():
        init_db()


@app.context_processor
def inject_cart_count():
    return {'cart_count': len(session.get('cart', []))}

# Public: menu index
@app.route('/')
def index():
    table = request.args.get('table', '')
    db = get_db()
    cur = db.execute('SELECT DISTINCT category FROM items WHERE available=1')
    categories = [r['category'] for r in cur.fetchall()]
    return render_template('index.html', restaurant=RESTAURANT_NAME, categories=categories, table=table)

@app.route('/category/<name>')
def category(name):
    table = request.args.get('table', '')
    db = get_db()
    cur = db.execute('SELECT * FROM items WHERE category=? AND available=1', (name,))
    items = cur.fetchall()
    return render_template('category.html', restaurant=RESTAURANT_NAME, category=name, items=items, table=table)

@app.route('/item/<int:item_id>')
def item_detail(item_id):
    table = request.args.get('table', '')
    db = get_db()
    cur = db.execute('SELECT * FROM items WHERE id=?', (item_id,))
    item = cur.fetchone()
    return render_template('item.html', restaurant=RESTAURANT_NAME, item=item, table=table)

# Cart stored in session
@app.route('/cart')
def cart():
    table = request.args.get('table', '')
    cart = session.get('cart', [])
    total = sum(i['price'] * i['qty'] for i in cart)
    return render_template('cart.html', restaurant=RESTAURANT_NAME, cart=cart, total=total, table=table)

@app.route('/cart/add', methods=['POST'])
def cart_add():
    # accept JSON or form-encoded data; avoid raising on non-json content types
    data = request.get_json(silent=True) or request.form or {}
    try:
        item_id = int(data.get('item_id'))
    except Exception:
        return jsonify({'error': 'Invalid item_id'}), 400
    try:
        qty = int(data.get('qty', 1))
    except Exception:
        qty = 1
    db = get_db()
    cur = db.execute('SELECT * FROM items WHERE id=?', (item_id,))
    item = cur.fetchone()
    if not item:
        return jsonify({'error': 'Not found'}), 404
    cart = session.get('cart', [])
    # merge if exists
    for it in cart:
        if it['item_id'] == item_id:
            it['qty'] += qty
            break
    else:
        cart.append({'item_id': item_id, 'name': item['name'], 'price': item['price'], 'qty': qty})
    session['cart'] = cart
    return jsonify({'ok': True, 'cart': cart})

@app.route('/order/create', methods=['POST'])
def order_create():
    # accept JSON or form data; use silent get_json to avoid errors on wrong content-type
    data = request.get_json(silent=True) or request.form or {}
    table_number = data.get('table') or ''
    customer_name = data.get('name')
    customer_phone = data.get('phone')
    cart = session.get('cart', [])
    if not cart:
        return jsonify({'error': 'Cart empty'}), 400
    total = sum(i['price'] * i['qty'] for i in cart)
    db = get_db()
    cur = db.cursor()
    cur.execute('INSERT INTO orders (table_number, status, total, created_at, customer_name, customer_phone) VALUES (?, ?, ?, ?, ?, ?)',
                (table_number, 'new', total, datetime.utcnow().isoformat(), customer_name, customer_phone))
    order_id = cur.lastrowid
    for it in cart:
        cur.execute('INSERT INTO order_items (order_id, item_id, name, qty, price) VALUES (?,?,?,?,?)',
                    (order_id, it['item_id'], it['name'], it['qty'], it['price']))
    db.commit()
    # notify admin via socket
    socketio.emit('new_order', {'order_id': order_id, 'table': table_number, 'total': total})
    # clear session cart
    session['cart'] = []
    # If form-based POST (not JSON), redirect to thank-you page
    if not request.json:
        return redirect(url_for('order_thanks', order_id=order_id))
    return jsonify({'ok': True, 'order_id': order_id})


@app.route('/order/checkout', methods=['GET'])
def order_checkout():
    table = request.args.get('table', '')
    cart = session.get('cart', [])
    if not cart:
        return redirect(url_for('index'))
    total = sum(i['price'] * i['qty'] for i in cart)
    return render_template('checkout.html', restaurant=RESTAURANT_NAME, cart=cart, total=total, table=table)


@app.route('/order/thanks/<int:order_id>')
def order_thanks(order_id):
    return render_template('thanks.html', order_id=order_id, restaurant=RESTAURANT_NAME)

# Admin
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        pw = request.form.get('password')
        if pw == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error='Wrong password')
    return render_template('login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('index'))

def admin_required(func):
    from functools import wraps
    @wraps(func)
    def wrapped(*a, **kw):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return func(*a, **kw)
    return wrapped

@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    cur = db.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 50')
    orders = cur.fetchall()
    return render_template('admin.html', orders=orders)

@app.route('/admin/items', methods=['GET', 'POST'])
@admin_required
def admin_items():
    db = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        category = request.form.get('category')
        price = float(request.form.get('price') or 0)
        desc = request.form.get('description')
        image_filename = None
        img = request.files.get('image')
        if img and img.filename:
            fn = secure_filename(img.filename)
            ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
            if ext in ALLOWED_EXT:
                # prevent name collision
                fname = f"{int(datetime.utcnow().timestamp())}_{fn}"
                path = os.path.join(UPLOAD_FOLDER, fname)
                img.save(path)
                image_filename = fname
        db.execute('INSERT INTO items (name, category, description, price, available, image) VALUES (?,?,?,?,1,?)',
                   (name, category, desc, price, image_filename))
        db.commit()
        return redirect(url_for('admin_items'))
    cur = db.execute('SELECT * FROM items')
    items = cur.fetchall()
    return render_template('admin_items.html', items=items)

@app.route('/admin/item/toggle/<int:item_id>', methods=['POST'])
@admin_required
def admin_toggle(item_id):
    db = get_db()
    cur = db.execute('SELECT available FROM items WHERE id=?', (item_id,))
    r = cur.fetchone()
    if not r:
        return ('', 404)
    new = 0 if r['available'] else 1
    db.execute('UPDATE items SET available=? WHERE id=?', (new, item_id))
    db.commit()
    return ('', 204)


@app.route('/admin/order/update/<int:order_id>', methods=['POST'])
@admin_required
def admin_order_update(order_id):
    # allow changing order status from admin UI
    new_status = request.form.get('status')
    if not new_status:
        return ('', 400)
    db = get_db()
    db.execute('UPDATE orders SET status=? WHERE id=?', (new_status, order_id))
    db.commit()
    # notify clients about update
    socketio.emit('order_update', {'order_id': order_id, 'status': new_status})
    return ('', 204)

# Socket route (for admin to connect and listen)
@socketio.on('connect')
def on_connect():
    pass

if __name__ == '__main__':
    setup()
    socketio.run(app, debug=True)
