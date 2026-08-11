"""
AGM Motor Bakım Merkezi — Streamlit uygulaması.
Tek dosyalık, çok kullanıcılı, rol tabanlı, fotoğraf destekli motor bakım
takip sistemi. MongoDB Atlas'a bağlanır. Streamlit Community Cloud'da
Docker/Render gerekmeden doğrudan çalışır.
"""
import base64
import hashlib
import io
import json
import os
from datetime import datetime, date

import pandas as pd
import pymongo
import streamlit as st
from PIL import Image

st.set_page_config(page_title="AGM Motor Bakım Merkezi", page_icon="🔧", layout="wide")

KRITIK_ESIK = 100
YAKLASIYOR_ESIK = 250
STATUS_LABELS = {"gecikmis": "🔴 Gecikmiş", "kritik": "🟠 Kritik", "yaklasiyor": "🟡 Yaklaşıyor", "normal": "🟢 Normal"}
STATUS_COLORS = {"gecikmis": "#ef4a52", "kritik": "#f2994a", "yaklasiyor": "#f0c93d", "normal": "#2fb374"}
ROLES = ["yonetici", "planlamaci", "teknisyen", "goruntuleyici"]
ROLE_LABELS = {"yonetici": "Yönetici", "planlamaci": "Planlamacı", "teknisyen": "Teknisyen", "goruntuleyici": "Görüntüleyici"}


# ============================================================
# Veritabanı bağlantısı
# ============================================================
@st.cache_resource
def get_db():
    uri = st.secrets["MONGO_URI"]
    db_name = st.secrets.get("MONGO_DB_NAME", "agm_bakim")
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client[db_name]


db = get_db()
engines_col = db["engines"]
types_col = db["maintenance_types"]
records_col = db["maintenance_records"]
users_col = db["users"]


def seed_if_empty():
    """
    Veritabanı boşsa (veya önceki bir deneme yarıda kalmışsa) V10 dosyasından
    çıkarılan gerçek verilerle doldurur. 'upsert' kullanır — aynı anda iki kez
    tetiklense veya kısmen daha önce çalışmış olsa bile hata vermez.
    """
    seed_path = os.path.join(os.path.dirname(__file__), "seed_data.json")
    with open(seed_path, encoding="utf-8") as f:
        data = json.load(f)

    if engines_col.count_documents({}) < len(data["engines"]):
        now = datetime.utcnow()
        for name, info in data["engines"].items():
            engines_col.update_one(
                {"_id": name},
                {"$setOnInsert": {
                    "name": name, "hours": info["hours"], "load_kw": info.get("load", 0),
                    "updated_at": now, "history": [{"date": now.isoformat(), "hours": info["hours"]}],
                }},
                upsert=True,
            )

    expected_type_count = 1 + len(data["maintTypes"])  # +1 = yağ
    if types_col.count_documents({}) < expected_type_count:
        oil_states = {name: {"last_maintenance_hour": rec["changeHour"], "period_hours": rec["maxHours"]}
                      for name, rec in data["oil"].items()}
        types_col.update_one(
            {"_id": "oil"},
            {"$setOnInsert": {"key": "oil", "label": "Yağ Değişimi", "default_period_hours": 700, "engine_states": oil_states}},
            upsert=True,
        )
        for mt in data["maintTypes"]:
            states = {name: {"last_maintenance_hour": rec["lastHour"], "period_hours": rec["period"]}
                      for name, rec in mt["perEngine"].items()}
            default_period = next(iter(states.values()))["period_hours"] if states else 0
            types_col.update_one(
                {"_id": mt["key"]},
                {"$setOnInsert": {"key": mt["key"], "label": mt["label"],
                                   "default_period_hours": default_period, "engine_states": states}},
                upsert=True,
            )


seed_if_empty()


# ============================================================
# Durum hesaplama
# ============================================================
def remaining_hours(engine_hours, last_hour, period):
    return period - (engine_hours - last_hour)


def status_for(remaining):
    if remaining <= 0:
        return "gecikmis"
    if remaining <= KRITIK_ESIK:
        return "kritik"
    if remaining <= YAKLASIYOR_ESIK:
        return "yaklasiyor"
    return "normal"


@st.cache_data(ttl=15)
def build_items():
    engines = {e["_id"]: e for e in engines_col.find()}
    types = list(types_col.find())
    items = []
    for t in types:
        states = t.get("engine_states", {})
        applicable = list(states.keys()) if states else list(engines.keys())
        for eng_id in applicable:
            engine = engines.get(eng_id)
            if not engine:
                continue
            state = states.get(eng_id, {})
            last_hour = state.get("last_maintenance_hour", 0)
            period = state.get("period_hours", t["default_period_hours"])
            remaining = remaining_hours(engine["hours"], last_hour, period)
            items.append({
                "engine_id": eng_id, "engine_name": engine["name"], "type_key": t["key"], "type_label": t["label"],
                "engine_hours": engine["hours"], "last_hour": last_hour, "period": period,
                "remaining": remaining, "status": status_for(remaining),
            })
    return items, engines, types


def engine_sort_key(name):
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


# ============================================================
# Kimlik doğrulama (basit, Streamlit oturumu üzerinden)
# ============================================================
def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
    return h, salt


def verify_password(password, salt, expected_hash):
    h, _ = hash_password(password, salt)
    return h == expected_hash


def create_user(full_name, email, password, role):
    salt_hash, salt = hash_password(password)
    users_col.insert_one({
        "_id": email.lower().strip(), "full_name": full_name, "email": email.lower().strip(),
        "password_hash": salt_hash, "salt": salt, "role": role, "active": True,
        "created_at": datetime.utcnow(),
    })


def login_view():
    st.markdown("## 🔧 AGM Motor Bakım Merkezi")
    user_count = users_col.count_documents({})

    if user_count == 0:
        st.info("Sistemde henüz kullanıcı yok. İlk kaydolan kişi otomatik olarak **yönetici** olur.")
        with st.form("ilk_kayit"):
            full_name = st.text_input("Adınız Soyadınız")
            email = st.text_input("E-posta")
            password = st.text_input("Şifre", type="password")
            submitted = st.form_submit_button("Yönetici Hesabı Oluştur", use_container_width=True)
            if submitted:
                if not full_name or not email or len(password) < 6:
                    st.error("Lütfen tüm alanları doldurun (şifre en az 6 karakter olmalı).")
                else:
                    create_user(full_name, email, password, "yonetici")
                    st.success("Hesap oluşturuldu! Şimdi giriş yapabilirsiniz.")
                    st.rerun()
        return

    tab_login, tab_register = st.tabs(["Giriş Yap", "Yeni Hesap (Teknisyen)"])

    with tab_login:
        with st.form("giris"):
            email = st.text_input("E-posta")
            password = st.text_input("Şifre", type="password")
            submitted = st.form_submit_button("Giriş Yap", use_container_width=True)
            if submitted:
                user = users_col.find_one({"_id": email.lower().strip()})
                if not user or not verify_password(password, user["salt"], user["password_hash"]):
                    st.error("E-posta veya şifre hatalı.")
                elif not user.get("active", True):
                    st.error("Hesabınız devre dışı bırakılmış. Yöneticinizle iletişime geçin.")
                else:
                    st.session_state.user = user
                    st.rerun()

    with tab_register:
        st.caption("Saha teknisyenleri buradan kendi hesabını oluşturabilir. Yönetici veya planlamacı yetkisi gerekiyorsa, giriş yaptıktan sonra bir yöneticiden 'Kullanıcılar' sayfasından rolünüzü değiştirmesini isteyin.")
        with st.form("kayit"):
            full_name = st.text_input("Adınız Soyadınız", key="reg_name")
            email = st.text_input("E-posta", key="reg_email")
            password = st.text_input("Şifre", type="password", key="reg_pass")
            submitted = st.form_submit_button("Hesap Oluştur", use_container_width=True)
            if submitted:
                if not full_name or not email or len(password) < 6:
                    st.error("Lütfen tüm alanları doldurun (şifre en az 6 karakter olmalı).")
                elif users_col.find_one({"_id": email.lower().strip()}):
                    st.error("Bu e-posta zaten kayıtlı.")
                else:
                    create_user(full_name, email, password, "teknisyen")
                    st.success("Hesap oluşturuldu! 'Giriş Yap' sekmesinden giriş yapabilirsiniz.")


# ============================================================
# Sayfalar
# ============================================================
def page_dashboard():
    items, engines, types = build_items()
    counts = {"gecikmis": 0, "kritik": 0, "yaklasiyor": 0, "normal": 0}
    for i in items:
        counts[i["status"]] += 1

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔴 Gecikmiş", counts["gecikmis"])
    c2.metric("🟠 Kritik", counts["kritik"])
    c3.metric("🟡 Yaklaşıyor", counts["yaklasiyor"])
    c4.metric("🟢 Normal", counts["normal"])

    st.markdown("### Motor Yükleri")
    load_rows = sorted(engines.values(), key=lambda e: engine_sort_key(e["name"]))
    total_load = sum(e.get("load_kw", 0) for e in load_rows)
    avg_load = total_load / len(load_rows) if load_rows else 0
    lc1, lc2 = st.columns(2)
    lc1.metric("Toplam Yük", f"{total_load:,.0f} kW")
    lc2.metric("Ortalama Yük", f"{avg_load:,.0f} kW")
    load_df = pd.DataFrame([{"Motor": e["name"], "Yük (kW)": e.get("load_kw", 0), "Çalışma Saati": e["hours"]} for e in load_rows])
    st.dataframe(load_df, use_container_width=True, hide_index=True, height=220)

    st.markdown("### Öncelikli Bakımlar")
    status_filter = st.selectbox("Duruma göre filtrele", ["Tümü", "Gecikmiş", "Kritik", "Yaklaşıyor", "Normal"], key="dash_filter")
    filter_map = {"Gecikmiş": "gecikmis", "Kritik": "kritik", "Yaklaşıyor": "yaklasiyor", "Normal": "normal"}

    rows = sorted(items, key=lambda i: i["remaining"])
    if status_filter != "Tümü":
        rows = [i for i in rows if i["status"] == filter_map[status_filter]]
    else:
        rows = [i for i in rows if i["remaining"] <= YAKLASIYOR_ESIK][:60]

    if not rows:
        st.success("Görüntülenecek bakım kaydı yok — her şey normal aralıkta.")
        return

    df = pd.DataFrame([{
        "Motor": r["engine_name"], "Bakım Türü": r["type_label"],
        "Kalan Saat": round(r["remaining"], 1), "Durum": STATUS_LABELS[r["status"]],
        "Motor Saati": r["engine_hours"], "Son Bakım Saati": r["last_hour"], "Periyot": r["period"],
    } for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True, height=460)


def page_hours_update():
    st.markdown("### Motor Çalışma Saatlerini Güncelle")
    st.caption("Bu ekrandan güncellediğiniz saatler, tüm bakım türlerindeki kalan süreleri otomatik olarak yeniden hesaplar.")

    engines = sorted(engines_col.find(), key=lambda e: engine_sort_key(e["name"]))
    with st.form("saat_guncelle"):
        new_values = {}
        cols = st.columns(3)
        for idx, e in enumerate(engines):
            with cols[idx % 3]:
                new_values[e["_id"]] = st.number_input(e["name"], value=float(e["hours"]), step=1.0, key=f"hr_{e['_id']}")
        submitted = st.form_submit_button("💾 Kaydet", use_container_width=True, type="primary")

    if submitted:
        stamp = datetime.utcnow()
        changed = 0
        for e in engines:
            new_val = new_values[e["_id"]]
            if new_val != e["hours"]:
                engines_col.update_one(
                    {"_id": e["_id"]},
                    {"$set": {"hours": new_val, "updated_at": stamp},
                     "$push": {"history": {"date": stamp.isoformat(), "hours": new_val}}},
                )
                changed += 1
        st.cache_data.clear()
        st.success(f"{changed} motor için çalışma saati güncellendi." if changed else "Değişiklik yapılmadı.")


def page_engines():
    items, engines, types = build_items()
    st.markdown("### Motorlar")

    query = st.text_input("Motor ara", placeholder="örn. AGM 12")
    status_filter = st.selectbox("Durum filtresi", ["Tümü", "Gecikmiş", "Kritik", "Yaklaşıyor", "Normal"])
    sort_by = st.radio("Sırala", ["Durum", "Motor No", "Çalışma Saati", "Yük"], horizontal=True)
    filter_map = {"Gecikmiş": "gecikmis", "Kritik": "kritik", "Yaklaşıyor": "yaklasiyor", "Normal": "normal"}
    status_order = {"gecikmis": 0, "kritik": 1, "yaklasiyor": 2, "normal": 3}

    rows = []
    for name, e in engines.items():
        if query and query.lower() not in name.lower():
            continue
        eng_items = sorted([i for i in items if i["engine_id"] == name], key=lambda i: i["remaining"])
        worst = eng_items[0] if eng_items else None
        status = worst["status"] if worst else "normal"
        gecikmis_n = sum(1 for i in eng_items if i["status"] == "gecikmis")
        kritik_n = sum(1 for i in eng_items if i["status"] == "kritik")
        rows.append({"name": name, "hours": e["hours"], "load": e.get("load_kw", 0),
                      "status": status, "gecikmis": gecikmis_n, "kritik": kritik_n,
                      "worst_remaining": worst["remaining"] if worst else 999999})

    if status_filter != "Tümü":
        rows = [r for r in rows if r["status"] == filter_map[status_filter]]

    if sort_by == "Durum":
        rows.sort(key=lambda r: (status_order[r["status"]], r["worst_remaining"]))
    elif sort_by == "Motor No":
        rows.sort(key=lambda r: engine_sort_key(r["name"]))
    elif sort_by == "Çalışma Saati":
        rows.sort(key=lambda r: -r["hours"])
    else:
        rows.sort(key=lambda r: -r["load"])

    if not rows:
        st.info("Eşleşen motor bulunamadı.")
        return

    df = pd.DataFrame([{
        "Motor": r["name"], "Durum": STATUS_LABELS[r["status"]],
        "Çalışma Saati": r["hours"], "Yük (kW)": r["load"],
        "Gecikmiş Bakım": r["gecikmis"], "Kritik Bakım": r["kritik"],
    } for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True, height=560)


def page_types():
    items, engines, types = build_items()
    st.markdown("### Bakım Türleri")

    type_options = {t["label"]: t["key"] for t in sorted(types, key=lambda t: t["label"])}
    selected_label = st.selectbox("Bakım türü seç", list(type_options.keys()))
    selected_key = type_options[selected_label]
    status_filter = st.selectbox("Durum filtresi", ["Tümü", "Gecikmiş", "Kritik", "Yaklaşıyor", "Normal"], key="types_status")
    filter_map = {"Gecikmiş": "gecikmis", "Kritik": "kritik", "Yaklaşıyor": "yaklasiyor", "Normal": "normal"}

    rows = [i for i in items if i["type_key"] == selected_key]
    if status_filter != "Tümü":
        rows = [r for r in rows if r["status"] == filter_map[status_filter]]
    rows.sort(key=lambda r: r["remaining"])

    if not rows:
        st.info("Kayıt bulunamadı.")
        return

    df = pd.DataFrame([{
        "Motor": r["engine_name"], "Motor Saati": r["engine_hours"], "Son Bakım Saati": r["last_hour"],
        "Periyot": r["period"], "Kalan Saat": round(r["remaining"], 1), "Durum": STATUS_LABELS[r["status"]],
    } for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True, height=520)


def compress_photo(uploaded_file, max_dim=720, quality=65):
    img = Image.open(uploaded_file)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        if w > h:
            new_w, new_h = max_dim, int(h * max_dim / w)
        else:
            new_w, new_h = int(w * max_dim / h), max_dim
        img = img.resize((new_w, new_h))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def page_complete_maintenance(current_user):
    if current_user["role"] == "goruntuleyici":
        st.warning("Görüntüleyici rolü bakım tamamlayamaz.")
        return

    items, engines, types = build_items()
    st.markdown("### Bakım Tamamla")

    engine_names = sorted(engines.keys(), key=engine_sort_key)
    engine_name = st.selectbox("Motor", engine_names)

    eng_items = sorted([i for i in items if i["engine_id"] == engine_name], key=lambda i: i["remaining"])
    if not eng_items:
        st.info("Bu motor için tanımlı bakım türü yok.")
        return

    option_labels = [f"{i['type_label']} · {STATUS_LABELS[i['status']]} · {round(i['remaining'])} sa" for i in eng_items]
    idx = st.selectbox("Bakım türü", range(len(eng_items)), format_func=lambda i: option_labels[i])
    chosen = eng_items[idx]

    st.info(f"**{chosen['type_label']}** — Motor saati: {chosen['engine_hours']} · Son bakım: {chosen['last_hour']} · Periyot: {chosen['period']}")

    with st.form("bakim_tamamla"):
        note = st.text_area("Ölçüm / açıklama (opsiyonel)")
        photo = st.file_uploader("Fotoğraf ekle (opsiyonel)", type=["jpg", "jpeg", "png", "webp"])
        camera_photo = st.camera_input("Ya da doğrudan fotoğraf çek (opsiyonel)")
        submitted = st.form_submit_button("✅ Bakımı Tamamla", use_container_width=True, type="primary")

    if submitted:
        photo_b64 = None
        chosen_photo = camera_photo or photo
        if chosen_photo is not None:
            try:
                photo_b64 = compress_photo(chosen_photo)
            except Exception:
                st.warning("Fotoğraf işlenemedi, kayıt fotoğrafsız kaydedildi.")

        engine_hours = engines[engine_name]["hours"]
        records_col.insert_one({
            "engine_id": engine_name, "engine_name": engine_name,
            "type_key": chosen["type_key"], "type_label": chosen["type_label"],
            "hour_at_completion": engine_hours, "note": note, "photo_b64": photo_b64,
            "technician_id": current_user["_id"], "technician_name": current_user["full_name"],
            "created_at": datetime.utcnow(),
        })

        if chosen["type_key"] == "oil":
            types_col.update_one({"_id": "oil"}, {"$set": {f"engine_states.{engine_name}.last_maintenance_hour": engine_hours}})
        else:
            types_col.update_one({"_id": chosen["type_key"]}, {"$set": {f"engine_states.{engine_name}.last_maintenance_hour": engine_hours}})

        st.cache_data.clear()
        st.success(f"{chosen['type_label']} bakımı {engine_name} için tamamlandı olarak kaydedildi.")
        st.rerun()


def page_records():
    st.markdown("### Bakım Kayıtları")
    records = list(records_col.find().sort("created_at", -1).limit(200))
    if not records:
        st.info("Henüz tamamlanmış bakım yok.")
        return

    for r in records:
        with st.container(border=True):
            cols = st.columns([1, 4]) if r.get("photo_b64") else [None, st.container()]
            if r.get("photo_b64"):
                with cols[0]:
                    st.image(base64.b64decode(r["photo_b64"]), use_container_width=True)
                info_col = cols[1]
            else:
                info_col = st.container()
            with info_col:
                st.markdown(f"**{r['type_label']}** · {r['engine_name']}")
                st.caption(f"{r['created_at'].strftime('%d.%m.%Y')} · {r['hour_at_completion']} sa okumasında · {r.get('technician_name','')}")
                if r.get("note"):
                    st.caption(f"📝 {r['note']}")


def page_hours_history():
    st.markdown("### Motor Saat Geçmişi")
    engines = sorted(engines_col.find(), key=lambda e: engine_sort_key(e["name"]))
    names = [e["name"] for e in engines]
    selected = st.selectbox("Motor seç", names)
    engine = next(e for e in engines if e["name"] == selected)
    history = sorted(engine.get("history", []), key=lambda h: h["date"])

    if len(history) < 2:
        st.info("Bu motor için henüz yeterli geçmiş kaydı yok.")
        return

    df = pd.DataFrame(history)
    df["date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={"date": "Tarih", "hours": "Saat"}).set_index("Tarih")
    st.line_chart(df)

    total_delta = history[-1]["hours"] - history[0]["hours"]
    span_days = max(1, (pd.to_datetime(history[-1]["date"]) - pd.to_datetime(history[0]["date"])).days)
    c1, c2, c3 = st.columns(3)
    c1.metric("Toplam Artış", f"{total_delta:,.0f} sa")
    c2.metric("Günlük Ortalama", f"{total_delta/span_days:,.1f} sa")
    c3.metric("Kayıt Sayısı", len(history))

    table_df = pd.DataFrame(history)[::-1]
    table_df["date"] = pd.to_datetime(table_df["date"]).dt.strftime("%d.%m.%Y")
    st.dataframe(table_df.rename(columns={"date": "Tarih", "hours": "Saat"}), use_container_width=True, hide_index=True)


def page_intervals():
    st.markdown("### Bakım Aralıkları")
    st.caption("Her motor + bakım türü için art arda tamamlanan bakımlar arasında geçen saat farkını gösterir.")
    records = list(records_col.find().sort("created_at", 1))
    if not records:
        st.info("Henüz tamamlanmış bakım yok. İlk bakımı kaydettiğinizde burada birikmeye başlayacak.")
        return

    groups = {}
    for r in records:
        key = (r["engine_name"], r["type_key"])
        groups.setdefault(key, {"label": r["type_label"], "entries": []})
        groups[key]["entries"].append(r)

    engine_filter = st.selectbox("Motora göre filtrele", ["Tümü"] + sorted({r["engine_name"] for r in records}, key=engine_sort_key))

    for (engine_name, type_key), g in sorted(groups.items(), key=lambda kv: engine_sort_key(kv[0][0])):
        if engine_filter != "Tümü" and engine_name != engine_filter:
            continue
        entries = g["entries"]
        st.markdown(f"**{engine_name} — {g['label']}**")
        rows = []
        prev = None
        for i, e in enumerate(entries):
            delta = None if prev is None else e["hour_at_completion"] - prev["hour_at_completion"]
            rows.append({
                "Tarih": e["created_at"].strftime("%d.%m.%Y"), "Motor Saati": e["hour_at_completion"],
                "Teknisyen": e.get("technician_name", ""),
                "Aralık": "MİLAD" if delta is None else f"{delta:,.0f} sa",
            })
            prev = e
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        if len(entries) >= 2:
            avg = (entries[-1]["hour_at_completion"] - entries[0]["hour_at_completion"]) / (len(entries) - 1)
            st.caption(f"Ortalama aralık: **{avg:,.0f} saat**")
        st.divider()


def page_export():
    st.markdown("### Excel Dışa / İçe Aktar")

    st.markdown("#### 📤 Rapor İndir")
    if st.button("Excel raporu oluştur"):
        items, engines, types = build_items()
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            eng_df = pd.DataFrame([{"MOTOR": e["name"], "MOTOR ÇALIŞMA SAATİ": e["hours"], "YÜK (kW)": e.get("load_kw", 0)}
                                    for e in sorted(engines.values(), key=lambda e: engine_sort_key(e["name"]))])
            eng_df.to_excel(writer, sheet_name="Motor Saatleri", index=False)

            summary_rows = sorted(items, key=lambda i: i["remaining"])
            summary_df = pd.DataFrame([{
                "MOTOR": i["engine_name"], "BAKIM TÜRÜ": i["type_label"], "MOTOR SAATİ": i["engine_hours"],
                "SON BAKIM SAATİ": i["last_hour"], "PERİYOT": i["period"],
                "KALAN SAAT": round(i["remaining"], 1), "DURUM": STATUS_LABELS[i["status"]],
            } for i in summary_rows])
            summary_df.to_excel(writer, sheet_name="Bakım Özeti", index=False)

            by_type = {}
            for i in items:
                by_type.setdefault(i["type_label"], []).append(i)
            for label, rows in by_type.items():
                rows = sorted(rows, key=lambda r: engine_sort_key(r["engine_name"]))
                df = pd.DataFrame([{
                    "MOTOR": r["engine_name"], "MOTOR SAATİ": r["engine_hours"], "SON BAKIM SAATİ": r["last_hour"],
                    "PERİYOT": r["period"], "KALAN SAAT": round(r["remaining"], 1), "DURUM": STATUS_LABELS[r["status"]],
                } for r in rows])
                df.to_excel(writer, sheet_name=label[:31], index=False)

        st.download_button("İndir (.xlsx)", buf.getvalue(),
                            file_name=f"AGM_Motor_Bakim_Raporu_{date.today().isoformat()}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("#### 📥 Motor Saatlerini İçe Aktar")
    st.caption("'MOTOR' ve 'MOTOR ÇALIŞMA SAATİ' sütunlarını içeren bir Excel dosyası yükleyin.")
    up = st.file_uploader("Excel dosyası seç", type=["xlsx", "xls"], key="import_file")
    if up is not None and st.button("İçe aktar"):
        df = pd.read_excel(up)
        cols = {c.upper(): c for c in df.columns}
        name_col = next((v for k, v in cols.items() if "MOTOR" in k and "SAAT" not in k and "YÜK" not in k), None)
        hour_col = next((v for k, v in cols.items() if "SAAT" in k), None)
        if not name_col or not hour_col:
            st.error("MOTOR ve MOTOR ÇALIŞMA SAATİ sütunları bulunamadı.")
        else:
            stamp = datetime.utcnow()
            updated = 0
            for _, row in df.iterrows():
                name, hours = str(row[name_col]).strip(), row[hour_col]
                if pd.isna(hours):
                    continue
                existing = engines_col.find_one({"_id": name})
                if existing and float(hours) != existing["hours"]:
                    engines_col.update_one(
                        {"_id": name},
                        {"$set": {"hours": float(hours), "updated_at": stamp},
                         "$push": {"history": {"date": stamp.isoformat(), "hours": float(hours)}}},
                    )
                    updated += 1
            st.cache_data.clear()
            st.success(f"{updated} motor için çalışma saati güncellendi.")


def page_users(current_user):
    if current_user["role"] != "yonetici":
        st.warning("Bu sayfa yalnızca yöneticiler içindir.")
        return
    st.markdown("### Kullanıcılar")
    users = list(users_col.find())
    for u in users:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
            c1.write(f"**{u['full_name']}**")
            c2.write(u["email"])
            new_role = c3.selectbox("Rol", ROLES, index=ROLES.index(u["role"]), format_func=lambda r: ROLE_LABELS[r],
                                     key=f"role_{u['_id']}", label_visibility="collapsed")
            if new_role != u["role"]:
                users_col.update_one({"_id": u["_id"]}, {"$set": {"role": new_role}})
                st.rerun()
            active = c4.checkbox("Aktif", value=u.get("active", True), key=f"active_{u['_id']}")
            if active != u.get("active", True):
                users_col.update_one({"_id": u["_id"]}, {"$set": {"active": active}})
                st.rerun()


# ============================================================
# Ana uygulama
# ============================================================
if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    login_view()
else:
    user = st.session_state.user
    with st.sidebar:
        st.markdown(f"**{user['full_name']}**")
        st.caption(ROLE_LABELS.get(user["role"], user["role"]))
        if st.button("Çıkış Yap", use_container_width=True):
            st.session_state.user = None
            st.rerun()
        st.divider()

        pages = ["📊 Özet", "⏱️ Saat Güncelle", "⚙️ Motorlar", "🔧 Bakım Türleri",
                 "✅ Bakım Tamamla", "📜 Bakım Kayıtları", "📈 Saat Geçmişi", "📊 Bakım Aralıkları", "📥 Excel"]
        if user["role"] == "yonetici":
            pages.append("👥 Kullanıcılar")
        choice = st.radio("Menü", pages, label_visibility="collapsed")

    if choice == "📊 Özet":
        page_dashboard()
    elif choice == "⏱️ Saat Güncelle":
        if user["role"] in ("yonetici", "planlamaci"):
            page_hours_update()
        else:
            st.warning("Bu işlem için yönetici veya planlamacı yetkisi gerekir.")
    elif choice == "⚙️ Motorlar":
        page_engines()
    elif choice == "🔧 Bakım Türleri":
        page_types()
    elif choice == "✅ Bakım Tamamla":
        page_complete_maintenance(user)
    elif choice == "📜 Bakım Kayıtları":
        page_records()
    elif choice == "📈 Saat Geçmişi":
        page_hours_history()
    elif choice == "📊 Bakım Aralıkları":
        page_intervals()
    elif choice == "📥 Excel":
        page_export()
    elif choice == "👥 Kullanıcılar":
        page_users(user)
