# -*- coding: utf-8 -*-
import asyncio, json, os, random, re, sqlite3, threading
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# ================= الإعدادات =================
BOT_TOKEN = "9107746305:AAH-FDUAV-toGqlbISxK0pSgiqE9o3Z3FQA"
API_ID = 34631956
API_HASH = "698f653e51872719991eb0834b81dbe3"
OWNER_ID = 7949959193
PASSWORD = "1981"
DB_PATH = "v18_database.db"
WORDS_FILE = "won.txt"

# ================= قاعدة البيانات =================
_lk = threading.Lock()
_c = None

def iso(): return datetime.utcnow().isoformat()

def db_init():
    global _c
    _c = sqlite3.connect(DB_PATH, check_same_thread=False)
    _c.row_factory = sqlite3.Row
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, verified INTEGER DEFAULT 0, session TEXT, phone TEXT, speed REAL DEFAULT 1, lines INTEGER DEFAULT 3, running INTEGER DEFAULT 0, sent_count INTEGER DEFAULT 0, target TEXT, banned INTEGER DEFAULT 0, role TEXT DEFAULT 'member', created_at TEXT);
    CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT, type TEXT, added_at TEXT);
    CREATE TABLE IF NOT EXISTS words(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER DEFAULT 0, word TEXT, UNIQUE(owner_id, word));
    CREATE TABLE IF NOT EXISTS state(user_id INTEGER PRIMARY KEY, action TEXT, data TEXT);
    """)
    cols = [r[1] for r in _c.execute("PRAGMA table_info(users)").fetchall()]
    if "banned" not in cols: _c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    if "role" not in cols: _c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'member'")
    _c.commit()

def q(sql, args=(), one=False, commit=False):
    with _lk:
        cur = _c.execute(sql, args)
        if commit: _c.commit(); return cur
        rows = cur.fetchall()
    return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]

def get_user(uid): return q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
def create_user(uid, un, fn):
    role = 'owner' if uid == OWNER_ID else 'member'
    q("INSERT OR IGNORE INTO users(user_id,username,first_name,role,created_at) VALUES(?,?,?,?,?)", (uid, un, fn, role, iso()), commit=True)
def set_verified(uid): q("UPDATE users SET verified=1 WHERE user_id=?", (uid,), commit=True)
def set_session(uid, s, p): q("UPDATE users SET session=?,phone=? WHERE user_id=?", (s, p, uid), commit=True)
def clear_session(uid): q("UPDATE users SET session=NULL,phone=NULL,running=0 WHERE user_id=?", (uid,), commit=True)
def set_speed(uid, v): q("UPDATE users SET speed=? WHERE user_id=?", (v, uid), commit=True)
def set_lines(uid, v): q("UPDATE users SET lines=? WHERE user_id=?", (v, uid), commit=True)
def set_running(uid, v): q("UPDATE users SET running=? WHERE user_id=?", (1 if v else 0, uid), commit=True)
def set_target(uid, t): q("UPDATE users SET target=? WHERE user_id=?", (t, uid), commit=True)
def inc_sent(uid): q("UPDATE users SET sent_count=sent_count+1 WHERE user_id=?", (uid,), commit=True)
def set_banned(uid, v): q("UPDATE users SET banned=? WHERE user_id=?", (1 if v else 0, uid), commit=True)
def set_role(uid, r): q("UPDATE users SET role=? WHERE user_id=?", (r, uid), commit=True)

def add_words(owner, text):
    n = 0
    for w in [p.strip() for p in re.split(r"[\s,\u060c\n]+", text) if p.strip()]:
        cur = q("INSERT OR IGNORE INTO words(owner_id, word) VALUES(?, ?)", (owner, w), commit=True)
        if cur.rowcount > 0: n += 1
    return n

def get_words(uid):
    db_w = [r["word"] for r in q("SELECT word FROM words WHERE owner_id = ?", (uid,))]
    file_w = []
    if os.path.exists(WORDS_FILE):
        try:
            with open(WORDS_FILE, "r", encoding="utf-8") as f:
                txt = f.read()
            file_w = [p.strip() for p in re.split(r"[\s,\u060c\n]+", txt) if p.strip()]
        except: pass
    return list(set(db_w + file_w))

def get_wcount(uid): return len(get_words(uid))
def get_all_wcount(): return q("SELECT COUNT(*) c FROM words", one=True)["c"]
def clear_user_words(): q("DELETE FROM words WHERE owner_id != 0", commit=True)
def get_ucount(): return q("SELECT COUNT(*) c FROM users", one=True)["c"]
def get_running_count(): return q("SELECT COUNT(*) c FROM users WHERE running=1", one=True)["c"]
def get_all_users(): return [r["user_id"] for r in q("SELECT user_id FROM users WHERE banned=0")]
def get_running_users(): return [r["user_id"] for r in q("SELECT user_id FROM users WHERE running=1")]

def set_state(uid, a, d=None): q("INSERT INTO state(user_id,action,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET action=excluded.action,data=excluded.data", (uid, a, json.dumps(d or {}, ensure_ascii=False)), commit=True)
def get_state(uid):
    r = q("SELECT * FROM state WHERE user_id=?", (uid,), one=True)
    if not r: return None
    try: d = json.loads(r["data"] or "{}")
    except: d = {}
    return {"action": r["action"], "data": d}
def clear_state(uid): q("DELETE FROM state WHERE user_id=?", (uid,), commit=True)

class UserbotManager:
    def __init__(self): self.clients = {}; self.pending = {}
    async def get(self, uid, session_str=None):
        c = self.clients.get(uid)
        if c:
            try:
                if c.is_connected(): return c
                await c.connect(); return c
            except: self.clients.pop(uid, None)
        if not session_str: return None
        try:
            c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            c.flood_sleep_threshold = 0
            await c.connect()
            if not await c.is_user_authorized():
                await c.disconnect(); return None
            self.clients[uid] = c; return c
        except: return None
    async def stop(self, uid):
        c = self.clients.pop(uid, None)
        if c:
            try: await c.disconnect()
            except: pass
    async def request_code(self, phone):
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        s = await c.send_code_request(phone)
        return c, s.phone_code_hash
    async def sign_in(self, c, phone, code, code_hash):
        try:
            await c.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
            return "ok"
        except SessionPasswordNeededError: return "2fa"
    async def sign_in_2fa(self, c, password): await c.sign_in(password=password)
    def save(self, c): return StringSession.save(c.session)

manager = UserbotManager()
def resolve_target(t): return int(t) if re.fullmatch(r"-?\d+", t or "") else t

class ClashEngine:
    def __init__(self): self.jobs = {}; self.used = {}
    def is_running(self, uid): return uid in self.jobs
    async def start(self, uid):
        if uid in self.jobs: return False
        u = get_user(uid)
        if not u or not u.get("session") or not u.get("target"): return False
        if uid != OWNER_ID and u["role"] != "admin": return False
        if get_wcount(uid) < 1: return False
        if not await manager.get(uid, u["session"]): return False
        set_running(uid, 1)
        self.jobs[uid] = asyncio.create_task(self.loop(uid))
        return True
    async def stop(self, uid):
        t = self.jobs.pop(uid, None)
        if t:
            t.cancel()
            try: await t
            except: pass
        set_running(uid, 0)
    def generate_text(self, words, lines, uid):
        used_set = self.used.setdefault(uid, set())
        if not words: return None
        for _ in range(5000):
            # عدد الكلمات حسب طلب المستخدم (الأسطر * 10) لضمان سطر طويل ومتواصل
            num_words = max(10, lines * 10)
            k = min(len(words), num_words)
            msg = " ".join(random.sample(words, k))
            if msg not in used_set:
                used_set.add(msg)
                if len(used_set) > 500000: used_set.clear()
                return msg
        return None
    async def loop(self, uid):
        try:
            while True:
                u = get_user(uid)
                if not u or not u.get("running"): break
                words = get_words(uid)
                if not words: break
                c = await manager.get(uid, u["session"])
                if not c: break
                msg = self.generate_text(words, max(1, int(u["lines"] or 1)), uid)
                if not msg:
                    await asyncio.sleep(1); continue
                try:
                    await c.send_message(resolve_target(u["target"]), msg)
                    inc_sent(uid)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1); continue
                except Exception as e:
                    print(f"Send error: {e}")
                    await asyncio.sleep(5)
                await asyncio.sleep(max(0.1, float(u["speed"] or 1)))
        except asyncio.CancelledError: pass
        except Exception as e: print(f"Loop error: {e}")
        finally:
            set_running(uid, 0)
            self.jobs.pop(uid, None)

engine = ClashEngine()

def get_main_menu(is_owner=False):
    kb = [
        [InlineKeyboardButton("🚀 تشغيل", callback_data="start"), InlineKeyboardButton("🔴 إيقاف", callback_data="stop")],
        [InlineKeyboardButton("🎯 تحديد هدف", callback_data="targets"), InlineKeyboardButton("🔗 ربط حسابي", callback_data="link")],
        [InlineKeyboardButton("💳 السيرفر", callback_data="server"), InlineKeyboardButton("⚡ زيادة سرعة", callback_data="speed")],
        [InlineKeyboardButton("📏 تكبير الكليشة", callback_data="lines"), InlineKeyboardButton("➕ إضافة كلمات", callback_data="addw")],
        [InlineKeyboardButton("📊 حالتي", callback_data="status")],
    ]
    if is_owner: kb.append([InlineKeyboardButton("👑 لوحة المالك", callback_data="owner")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu")])
    return InlineKeyboardMarkup(kb)

def get_back_kb(to="menu"): return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=to)]])
def get_owner_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 رفع أدمن", callback_data="promote"), InlineKeyboardButton("🔻 تنزيل أدمن", callback_data="demote")],
        [InlineKeyboardButton("🚫 حظر عضو", callback_data="ban"), InlineKeyboardButton("✅ فك الحظر", callback_data="unban")],
        [InlineKeyboardButton("📢 رسالة للجميع", callback_data="bc")],
        [InlineKeyboardButton("🗑 مسح الكلمات", callback_data="wclear")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu")],
    ])

def get_targets_kb(uid, active_target):
    rows = []
    for t in q("SELECT * FROM targets WHERE user_id=?", (uid,)):
        mark = "✅ " if t["target"] == active_target else ""
        rows.append([InlineKeyboardButton(mark + t["target"], callback_data=f"st:{t['id']}"), InlineKeyboardButton("🗑", callback_data=f"dt:{t['id']}")])
    rows.append([InlineKeyboardButton("➕ إضافة هدف", callback_data="addt")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

async def edit_msg(qq, text, kb=None):
    try: await qq.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except:
        try: await qq.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        except: pass

def get_main_text(uid):
    r = "🟢 شغال" if engine.is_running(uid) else "🔴 متوقف"
    return f"🎛 <b>بوت الكلايش V18</b>\n👤 آيديك: <code>{uid}</code>\n🚦 الحالة: {r}\n──────────────\nاختر من الأزرار:"

async def show_main(update, is_edit=False):
    uid = update.effective_user.id
    u = get_user(uid)
    is_owner = u and u["role"] == "owner"
    kb = get_main_menu(is_owner)
    txt = get_main_text(uid)
    if is_edit: await edit_msg(update.callback_query, txt, kb)
    else: await update.message.reply_text(txt, reply_markup=kb, parse_mode="HTML")

async def cmd_start(update, context):
    u = update.effective_user
    if not get_user(u.id): create_user(u.id, u.username, u.first_name)
    user = get_user(u.id)
    if user["banned"]:
        await update.message.reply_text("🚫 أنت محظور من استخدام البوت."); return
    if not user["verified"]:
        await update.message.reply_text("🔐 أرسل كلمة سر البوت للتحقق:")
        set_state(u.id, "await_password"); return
    await show_main(update)

async def cmd_menu(update, context): await show_main(update)

async def handle_text(update, context):
    u = update.effective_user
    if not get_user(u.id): create_user(u.id, u.username, u.first_name)
    user = get_user(u.id)
    t = (update.message.text or "").strip()
    if user["banned"]:
        await update.message.reply_text("🚫 أنت محظور."); return
    st = get_state(u.id)
    action = st["action"] if st else None
    data = st["data"] if st else {}
    if not user["verified"] or action == "await_password":
        if t == PASSWORD:
            set_verified(u.id); clear_state(u.id)
            await update.message.reply_text("✅ تم التحقق بنجاح!", reply_markup=get_main_menu(u.id == OWNER_ID or user["role"] == "owner"))
        else: await update.message.reply_text("🔐 كلمة سر خاطئة، حاول مرة أخرى:")
        return
    if not action:
        await update.message.reply_text("استخدم /start لعرض القائمة."); return
    if action == "phone":
        p = t.replace(" ", "")
        if not re.fullmatch(r"\+?\d{7,15}", p):
            await update.message.reply_text("❌ رقم غير صحيح. مثال: +964338******"); return
        try:
            client, code_hash = await manager.request_code(p)
            manager.pending[u.id] = client
            set_state(u.id, "code", {"phone": p, "code_hash": code_hash})
            await update.message.reply_text("📩 أرسل الكود الذي وصلك:")
        except Exception as e:
            clear_state(u.id); await update.message.reply_text(f"❌ فشل إرسال الكود: {e}")
    elif action == "code":
        client = manager.pending.get(u.id)
        if not client: clear_state(u.id); await update.message.reply_text("❌ انتهت الجلسة."); return
        try:
            res = await manager.sign_in(client, data.get("phone"), t, data.get("code_hash"))
            if res == "2fa":
                set_state(u.id, "2fa", data); await update.message.reply_text("🔒 حسابك محمي بكلمة سر (2FA). أرسلها:"); return
            await finalize_link(update, u.id, client, data.get("phone"))
        except Exception as e: await update.message.reply_text(f"❌ كود خاطئ أو خطأ: {e}")
    elif action == "2fa":
        client = manager.pending.get(u.id)
        if not client: clear_state(u.id); await update.message.reply_text("❌ انتهت الجلسة."); return
        try:
            await manager.sign_in_2fa(client, t)
            await finalize_link(update, u.id, client, data.get("phone"))
        except Exception as e: await update.message.reply_text(f"❌ كلمة سر خاطئة: {e}")
    elif action == "speed":
        try: v = float(t)
        except: await update.message.reply_text("❌ أرسل رقماً (مثال: 0.5 أو 2)."); return
        if v < 0.1: await update.message.reply_text("❌ السرعة يجب أن تكون 0.1 أو أكثر."); return
        set_speed(u.id, v); clear_state(u.id)
        await update.message.reply_text(f"✅ السرعة: {v} ثانية", reply_markup=get_main_menu(user["role"] == "owner"))
    elif action == "lines":
        try: v = int(t)
        except: await update.message.reply_text("❌ أرسل رقماً صحيحاً."); return
        if v < 1: await update.message.reply_text("❌ عدد الأسطر يجب أن يكون 1 أو أكثر."); return
        set_lines(u.id, v); clear_state(u.id)
        await update.message.reply_text(f"✅ عدد الأسطر: {v}", reply_markup=get_main_menu(user["role"] == "owner"))
    elif action == "target":
        raw = t; target = raw
        if "t.me/" in raw and not raw.startswith("@"):
            slug = raw.split("t.me/")[-1].strip("/").split("?")[0]
            if slug.startswith("+") or slug.startswith("joinchat"):
                await update.message.reply_text("❌ استخدم @username أو -100 أو رابط قناة/كروب عام."); return
            target = "@" + slug
        q("INSERT INTO targets(user_id, target, type, added_at) VALUES(?,?,?,?)", (u.id, target, "auto", iso()), commit=True)
        set_target(u.id, target); clear_state(u.id)
        await update.message.reply_text(f"✅ تم تحديد الهدف: {target}", reply_markup=get_main_menu(user["role"] == "owner"))
        if not user.get("session"): await update.message.reply_text("❌ تعذر التشغيل: حسابك غير مربوط.")
        elif get_wcount(u.id) < 1: await update.message.reply_text("❌ تعذر التشغيل: ما عندك كلمات.")
        else:
            ok = await engine.start(u.id)
            if ok: await update.message.reply_text("🚀 بدأ الإرسال تلقائياً!")
            else: await update.message.reply_text("❌ تعذر التشغيل: في مشكلة بالربط أو الهدف.")
    elif action == "addw":
        n = add_words(u.id, t); clear_state(u.id)
        await update.message.reply_text(f"✅ أضفت {n} كلمة خاصة بك.", reply_markup=get_main_menu(user["role"] == "owner"))
    elif action in ("promote", "demote", "ban", "unban"):
        if user["role"] != "owner": return
        target_id = None
        if t.startswith("@"):
            r = q("SELECT user_id FROM users WHERE username=?", (t[1:],), one=True)
            if r: target_id = r["user_id"]
        elif t.isdigit(): target_id = int(t)
        if not target_id: await update.message.reply_text("❌ لم يتم العثور على المستخدم."); return
        msg = ""
        if action == "promote": set_role(target_id, "admin"); msg = "👑 تم رفعه أدمن."
        elif action == "demote": set_role(target_id, "member"); msg = "🔻 تم تنزيله عضو."
        elif action == "ban": set_banned(target_id, 1); msg = "🚫 تم حظره."
        elif action == "unban": set_banned(target_id, 0); msg = "✅ تم فك الحظر."
        clear_state(u.id); await update.message.reply_text(msg, reply_markup=get_owner_kb())
    elif action == "bc":
        if user["role"] != "owner": return
        clear_state(u.id)
        ids = get_all_users(); ok = 0
        for uid in ids:
            try:
                await context.bot.send_message(uid, f"📢 <b>رسالة من المالك:</b>\n\n{t}", parse_mode="HTML")
                ok += 1; await asyncio.sleep(0.05)
            except: pass
        await update.message.reply_text(f"✅ تم إرسال الرسالة لـ {ok} مستخدم.", reply_markup=get_owner_kb())
    else:
        clear_state(u.id); await update.message.reply_text("استخدم /start.")

async def finalize_link(update, uid, client, phone):
    try: session_str = manager.save(client)
    except Exception as e:
        await update.message.reply_text(f"❌ تعذر حفظ الجلسة: {e}"); return
    set_session(uid, session_str, phone)
    manager.pending.pop(uid, None); manager.clients[uid] = client; clear_state(uid)
    user = get_user(uid)
    await update.message.reply_text(f"✅ تم ربط الحساب بنجاح!\n📱 {phone}", reply_markup=get_main_menu(user["role"] == "owner"))

async def handle_callback(update, context):
    qq = update.callback_query; await qq.answer(); uid = qq.from_user.id
    if not get_user(uid): create_user(uid, qq.from_user.username, qq.from_user.first_name)
    user = get_user(uid)
    if user["banned"]: await edit_msg(qq, "🚫 أنت محظور."); return
    if not user["verified"]: await edit_msg(qq, "🔐 أرسل كلمة السر أولاً عبر /start"); return
    d = qq.data; is_owner = user["role"] == "owner"
    if d == "menu": await show_main(update, is_edit=True)
    elif d == "start":
        if not user.get("session"): await edit_msg(qq, "❌ اربط حسابك أولاً.", get_main_menu(is_owner)); return
        if not user.get("target"): await edit_msg(qq, "❌ حدد هدفاً أولاً.", get_main_menu(is_owner)); return
        if engine.is_running(uid): await edit_msg(qq, "⚠️ الكلايش شغال بالفعل.", get_main_menu(is_owner)); return
        ok = await engine.start(uid); u2 = get_user(uid)
        if ok: await edit_msg(qq, f"🚀 تم التشغيل!\n🎯 {u2['target']}\n⚙️ {u2['speed']} ثانية\n📝 {u2['lines']} سطر", get_main_menu(is_owner))
        else: await edit_msg(qq, "❌ تعذر التشغيل.", get_main_menu(is_owner))
    elif d == "stop":
        if engine.is_running(uid): await engine.stop(uid); await edit_msg(qq, "🔴 تم الإيقاف.", get_main_menu(is_owner))
        else: await edit_msg(qq, "متوقف بالفعل.", get_main_menu(is_owner))
    elif d == "targets":
        u2 = get_user(uid); await edit_msg(qq, "🎯 <b>تحديد هدف</b>\nاختر هدفاً أو 🗑 للحذف:", get_targets_kb(uid, u2.get("target") or ""))
    elif d == "addt":
        set_state(uid, "target")
        await edit_msg(qq, "حاضر، أرسل الآيدي للمجموعة أو الرابط أو يوزر شخص:", get_back_kb("targets"))
    elif d.startswith("st:"):
        tid = int(d[3:]); t = q("SELECT * FROM targets WHERE id=? AND user_id=?", (tid, uid), one=True)
        if t: set_target(uid, t["target"])
        u2 = get_user(uid); await edit_msg(qq, "🎯 <b>تحديد هدف</b>", get_targets_kb(uid, u2.get("target") or ""))
    elif d.startswith("dt:"):
        tid = int(d[3:]); r = q("SELECT target FROM targets WHERE id=? AND user_id=?", (tid, uid), one=True)
        q("DELETE FROM targets WHERE id=? AND user_id=?", (tid, uid), commit=True)
        if r and get_user(uid).get("target") == r["target"]: set_target(uid, "")
        u2 = get_user(uid); await edit_msg(qq, "🎯 <b>تحديد هدف</b>", get_targets_kb(uid, u2.get("target") or ""))
    elif d == "link":
        if user.get("session"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 فصل الحساب", callback_data="unlink")], [InlineKeyboardButton("🔙 رجوع", callback_data="menu")]])
            await edit_msg(qq, f"✅ حسابك مربوط.\n📱 {user.get('phone') or '—'}", kb)
        else:
            set_state(uid, "phone"); await edit_msg(qq, "🔗 أرسل رقمك معه رمز الدولة (+964...):", get_back_kb())
    elif d == "unlink":
        await engine.stop(uid); await manager.stop(uid); clear_session(uid)
        await edit_msg(qq, "✅ تم فصل الحساب.", get_main_menu(is_owner))
    elif d == "server":
        await edit_msg(qq, "💳 <b>الاشتراكات</b>\n\nللاشتراك تواصل معه المالك: @P71GP", get_back_kb())
        try: await context.bot.send_message(OWNER_ID, f"🔔 <b>إشعار جديد:</b>\n\nالمستخدم: {user.get('first_name')}\nالآيدي: <code>{uid}</code>\nيريد الاشتراك!", parse_mode="HTML")
        except: pass
    elif d == "speed":
        set_state(uid, "speed"); await edit_msg(qq, "⚡ أرسل السرعة بالثواني (مثال: 0.5):", get_back_kb())
    elif d == "lines":
        set_state(uid, "lines"); await edit_msg(qq, "📏 أرسل عدد الأسطر المطلوبة:", get_back_kb())
    elif d == "addw":
        set_state(uid, "addw"); await edit_msg(qq, "➕ أرسل الكلمات مفصولة بمسافة أو فاصلة أو سطر:", get_back_kb())
    elif d == "status":
        u2 = get_user(uid); r = "🟢 شغال" if engine.is_running(uid) else "🔴 متوقف"
        linked = "✅ مربوط" if u2.get("session") else "❌ غير مربوط"
        txt = (f"📊 <b>حالتك</b>\n🆔 <code>{uid}</code>\n👤 الاسم: {u2.get('first_name') or '—'}\n"
               f"🔗 الحساب: {linked}\n🚦 الكلايش: {r}\n📨 المرسلة: {u2['sent_count']}\n"
               f"🎯 الهدف: <code>{u2.get('target') or '—'}</code>\n⚡ السرعة: {u2['speed']} ثانية\n"
               f"📏 الأسطر: {u2['lines']}\n📚 الكلمات: {get_wcount(uid)}")
        try:
            photos = await context.bot.get_user_profile_photos(uid, limit=1)
            if photos.total_count > 0:
                await qq.message.reply_photo(photos.photos[0][-1].file_id, caption=txt, parse_mode="HTML", reply_markup=get_main_menu(is_owner))
            else: await edit_msg(qq, txt, get_main_menu(is_owner))
        except: await edit_msg(qq, txt, get_main_menu(is_owner))
    elif d == "owner":
        if not is_owner: return
        txt = (f"👑 <b>لوحة المالك V18</b>\n👥 المستخدمون: {get_ucount()}\n"
               f"🟢 المشغلون: {get_running_count()}\n📚 الكلمات: {get_all_wcount()}")
        await edit_msg(qq, txt, get_owner_kb())
    elif d in ("promote", "demote", "ban", "unban"):
        if not is_owner: return
        set_state(uid, d)
        titles = {"promote": "رفع أدمن", "demote": "تنزيل أدمن", "ban": "حظر", "unban": "فك الحظر"}
        await edit_msg(qq, f"أرسل آيدي أو يوزر المستخدم ({titles[d]}):", get_back_kb("owner"))
    elif d == "wclear":
        if not is_owner: return
        clear_user_words(); await edit_msg(qq, "✅ تم مسح جميع الكلمات المضافة.", get_owner_kb())
    elif d == "bc":
        if not is_owner: return
        set_state(uid, "bc"); await edit_msg(qq, "📢 أرسل الرسالة التي تريد إرسالها للجميع:", get_back_kb("owner"))

async def resume_jobs(app):
    for uid in get_running_users():
        try:
            ok = await engine.start(uid)
            if not ok: set_running(uid, 0)
        except: set_running(uid, 0)

def main():
    db_init()
    app = Application.builder().token(BOT_TOKEN).post_init(resume_jobs).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("✅ V18 يعمل الآن...")
    app.run_polling()

if __name__ == "__main__":
    main()
