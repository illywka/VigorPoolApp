import streamlit as st
from tuya_connector import TuyaOpenAPI
import base64
import struct
import time
import requests
import threading
import pandas as pd

st.set_page_config(page_title="VigorMonitor", page_icon="⚡", layout="centered")

# --- 1. СПІЛЬНА ПАМ'ЯТЬ ---
@st.cache_resource
class SharedStorage:
    def __init__(self):
        self.data = None
        self.last_update = 0
        self.telegram_offset = 0
        self.was_online = None 
        self.zero_counter = 0
        self.pending_cmd = None 
        self.last_in_val = -1
        self.last_in_change = 0
        self.last_out_val = -1
        self.last_out_change = 0
        self.history = []

storage = SharedStorage()

# --- 2. ЛОГІКА ДЕКОДУВАННЯ ---
def get_vigor_state(api_result):
    s = { "battery": 0, "temp": 0, "in_watts": 0, "out_watts": 0, "time_left": 0, "is_charging": False, "fast_mode": False }
    s['battery'] = next((i['value'] for i in api_result if i['code'] == 'battery_percentage'), 0)
    s['temp'] = next((i['value'] for i in api_result if i['code'] == 'temp_current'), 0)
    s['fast_mode'] = next((i['value'] for i in api_result if i['code'] == 'pd_switch_1'), False)

    # Декодування зарядки
    p_in, t_full = 0, 0
    c_data = next((i['value'] for i in api_result if i['code'] == 'charged_data'), None)
    if c_data and c_data != "yAAAAFYAAAA=":
        try:
            raw = base64.b64decode(c_data)
            p_in, t_full = struct.unpack('<ii', raw[:8])
            s['in_watts'] = p_in
        except: pass

    # Декодування розрядки
    d_data = next((i['value'] for i in api_result if i['code'] == 'battery_parameters'), None)
    if d_data:
        try:
            raw = base64.b64decode(d_data)
            p_out, _, t_empty = struct.unpack('<iii', raw[:12])
            s['out_watts'] = p_out
            # Вибір часу (до повного або до нуля)
            s['time_left'] = t_full if p_in > p_out else t_empty
        except: pass

    s['is_charging'] = s['in_watts'] > 5
    return s

def worker_tuya():
    api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
    while True:
        try:
            if not api.is_connect():
                api.connect()
            
            res = api.get(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/status")
            if res.get('success'):
                new_s = get_vigor_state(res['result'])
                curr_time = time.time() # Системний час для логіки

                # === 1. WATCHDOG (ВБИВЦЯ ЗАЛИПАННЯ) ===
                if new_s['in_watts'] != storage.last_in_val:
                    storage.last_in_val = new_s['in_watts']
                    storage.last_in_change = curr_time
                elif new_s['in_watts'] > 0 and (curr_time - storage.last_in_change) > 300:
                    new_s['in_watts'] = 0

                if new_s['out_watts'] != storage.last_out_val:
                    storage.last_out_val = new_s['out_watts']
                    storage.last_out_change = curr_time
                elif new_s['out_watts'] > 0 and (curr_time - storage.last_out_change) > 120:
                    new_s['out_watts'] = 0

                # === 2. ЛОГІКА СПОВІЩЕНЬ (Світло Є / Немає) ===
                # Визначаємо статус "Світло є" (якщо вхід > 405 Вт або просто > 5 Вт для точності)
                has_power = (new_s['in_watts'] > 405) 
                
                if storage.was_online is None:
                    storage.was_online = has_power
                elif has_power != storage.was_online:
                    # Якщо світло зникло, чекаємо 2 цикли (zero_counter), щоб уникнути помилкових спрацювань
                    if not has_power:
                        storage.zero_counter += 1
                    else:
                        storage.zero_counter = 0
                    
                    if has_power or storage.zero_counter >= 2:
                        storage.was_online = has_power
                        storage.zero_counter = 0
                        if has_power:
                            send_telegram_bg("⚡ Світло Є!")
                        else:
                            send_telegram_bg(f"🪫 Зарядка закінчилась або світло зникло. ({new_s['battery']}%)")

                # === 3. ЗБЕРЕЖЕННЯ ДАНИХ ТА ГРАФІК ===
                storage.data = new_s
                storage.last_update = curr_time

                # Додаємо в історію з київським часом (+2 год)
                storage.history.append({
                    "time": pd.to_datetime(curr_time + 7200, unit='s'),
                    "Вхід (W)": new_s['in_watts'],
                    "Вихід (W)": new_s['out_watts']
                })
                if len(storage.history) > 100: storage.history.pop(0)

                # === 4. АДАПТИВНИЙ СОН ===
                sleep_time = 20 if (new_s['in_watts'] > 0 or new_s['out_watts'] > 0) else 120
            else:
                sleep_time = 300 # Якщо станція офлайн, спимо 5 хв
            
            time.sleep(sleep_time)
        except Exception as e:
            time.sleep(30)

# --- 5. ФРОНТЕНД ---
@st.fragment(run_every=1)
def monitorPage():
    s = storage.data
    if s is None:
        st.info("📡 Очікування даних від Tuya...")
        return

    # Динамічний колір відсотка
    bat_color = "#00c853" if s['battery'] > 20 else "#ff4b4b"
    st.markdown(f"<h1 style='text-align: center; font-size: 80px; color: {bat_color};'>{s['battery']}%</h1>", unsafe_allow_html=True)
    
    # Розрахунок часу оновлення (Київ +2)
    curr = time.time() + 7200
    upd_ts = storage.last_update + 7200
    time_str = time.strftime("%H:%M:%S", time.localtime(upd_ts))
    ago = int(curr - upd_ts)
    
    status_text = "⚡ Заряджається" if s['is_charging'] else "🔋 Від батареї"
    st.markdown(f"<p style='text-align: center; color: gray;'>{status_text} | {time_str} ({ago}с тому)</p>", unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Вхід", f"{s['in_watts']} W")
    c2.metric("Вихід", f"{s['out_watts']} W")
    h, m = s['time_left'] // 3600, (s['time_left'] % 3600) // 60
    c3.metric("Залишилось", f"{h}г {m:02d}хв")
    c4.metric("Темп.", f"{s['temp']}°C")

    if storage.history:
        df = pd.DataFrame(storage.history)
        st.line_chart(df, x="time", y=["Вхід (W)", "Вихід (W)"], color=["#00c853", "#ff4b4b"])

# Решта функцій (main, settingsPage, start_threads) залишаються як були
# --- 4. ПОТІК 2: TELEGRAM ---
def worker_telegram():
    while True:
        try:
            if storage.data is None:
                time.sleep(2)
                continue

            raw_users = st.secrets.get("ALLOWED_USERS", st.secrets["CHAT_ID"])
            allowed_list = [u.strip() for u in raw_users.split(",")] 
            token = st.secrets["BOT_TOKEN"]
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": storage.telegram_offset + 1, "timeout": 10}
            
            resp = requests.get(url, params=params, timeout=15).json()

            if resp.get('ok') and resp.get('result'):
                for update in resp['result']:
                    storage.telegram_offset = update['update_id']
                    msg = update.get('message', {})
                    text = msg.get('text', '').lower()
                    cid = str(msg.get('chat', {}).get('id', ''))
                    
                    if cid in allowed_list:
                        if "/status" in text or "статус" in text or "start" in text:
                            s = storage.data
                            upd = time.strftime(f"%d.%m %H:%M:%S", time.localtime(storage.last_update))
                            h = s['time_left'] // 3600
                            m = (s['time_left'] % 3600) // 60
                            display_time = f"{h}г {m:02d}хв"
                            reply = (
                                f"Батарея: {s['battery']}%\n\n"
                                f"🟢 Вхід: {s['in_watts']} W\n"
                                f"🔌 Вихід: {s['out_watts']} W\n"
                                f"Часу залишилось: {display_time}\n\n"
                                f"Оновлено {upd}"
                            )
                            send_telegram_bg(reply, target_id=cid)
            time.sleep(1)
        except:
            time.sleep(5)

def send_telegram_bg(message, target_id=None):
    try:
        token = st.secrets["BOT_TOKEN"]
        chat_id = target_id if target_id else st.secrets["CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      data={"chat_id": chat_id, "text": message}, timeout=5)
    except: pass


@st.cache_resource
def start_threads():
    threading.Thread(target=worker_tuya, daemon=True).start()
    threading.Thread(target=worker_telegram, daemon=True).start()

start_threads()

# --- 5. ФРОНТЕНД ---

def queue_speed_command(is_slow):
    storage.pending_cmd = (is_slow, time.time())
    st.toast(f"Команду додано в чергу!", icon="⏳")


def settingsPage(s):
    if s is None:
        st.info("Зачекайте, дані завантажуються...")
        return
    real = "Повільна" if s['fast_mode'] else "Швидка"
    if 'fake_val' not in st.session_state: st.session_state['fake_val'] = real
    if 'last_click' not in st.session_state: st.session_state['last_click'] = 0
    disp = st.session_state['fake_val'] if (time.time() - st.session_state['last_click'] < 5) else real
    sel = st.select_slider("Режим зарядки:", ["Повільна", "Швидка"], value=disp)
    if sel != disp:
        st.session_state['last_click'] = time.time()
        st.session_state['fake_val'] = sel
        queue_speed_command(sel == "Повільна")
        st.rerun()

def main():
    s = storage.data
    monitor, settings = st.tabs(["Моніторинг", "Керування"])
    with monitor: monitorPage()
    with settings: settingsPage(s)

if __name__ == "__main__":
    main()