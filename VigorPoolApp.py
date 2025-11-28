import streamlit as st
from tuya_connector import TuyaOpenAPI
import base64
import struct
import time
import requests
import threading

st.set_page_config(page_title="VigorMonitor Ultimate", page_icon="⚡", layout="centered")

# --- 1. СПІЛЬНА ПАМ'ЯТЬ ---
@st.cache_resource
class SharedStorage:
    def __init__(self):
        self.data = None
        self.last_update = 0
        self.last_heartbeat = 0
        self.telegram_offset = 0
        self.was_online = None 
        self.zero_counter = 0
        
        # --- НОВЕ: ЧЕРГА КОМАНД ---
        # Зберігаємо кортеж: (значення_pd_switch, час_створення)
        # Наприклад: (True, 1700000000.0)
        self.pending_cmd = None 

storage = SharedStorage()

# --- 2. ЛОГІКА ДЕКОДУВАННЯ ---
def get_vigor_state(api_result):
    s = { "battery": 0, "temp": 0, "in_watts": 0, "out_watts": 0, "time_left": 0, "is_charging": False, "fast_mode": False }
    s['battery'] = next((i['value'] for i in api_result if i['code'] == 'battery_percentage'), 0)
    s['temp'] = next((i['value'] for i in api_result if i['code'] == 'temp_current'), 0)
    s['fast_mode'] = next((i['value'] for i in api_result if i['code'] == 'pd_switch_1'), False)

    c_data = next((i['value'] for i in api_result if i['code'] == 'charged_data'), None)
    if c_data:
        if c_data == "yAAAAFYAAAA=": 
            s['in_watts'] = 0
        else:
            try:
                raw = base64.b64decode(c_data)
                p_in, t_full = struct.unpack('<ii', raw[:8])
                s['in_watts'] = p_in
                if p_in > 0:
                    s['is_charging'] = True
                    s['time_left'] = t_full
            except: pass

    d_data = next((i['value'] for i in api_result if i['code'] == 'battery_parameters'), None)
    if d_data:
        try:
            raw = base64.b64decode(d_data)
            p_out, _, t_empty = struct.unpack('<iii', raw[:12])
            s['out_watts'] = p_out
            if not s['is_charging']:
                s['time_left'] = t_empty
        except: pass
    return s

def send_telegram_bg(message, target_id=None):
    try:
        token = st.secrets["BOT_TOKEN"]
        chat_id = target_id if target_id else st.secrets["CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      data={"chat_id": chat_id, "text": message}, timeout=5)
    except: pass

# --- 3. ПОТІК 1: TUYA + ВИКОНАВЕЦЬ КОМАНД ---
def worker_tuya():
    while True:
        try:
            api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
            api.connect()
            
            res = api.get(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/status")
            
            storage.last_heartbeat = time.time()
            
            if res['success']:
                new_s = get_vigor_state(res['result'])
                
                # --- А. ОНОВЛЕННЯ ДАНИХ ---
                if storage.data is None or storage.data != new_s:
                    storage.data = new_s
                    storage.last_update = time.time()
                
                # --- Б. ПЕРЕВІРКА ВІДКЛАДЕНИХ КОМАНД (НОВЕ!) ---
                if storage.pending_cmd is not None:
                    target_val, created_at = storage.pending_cmd
                    
                    # Перевіряємо, чи не протухла команда (10 хвилин = 600 сек)
                    if (time.time() - created_at) < 300:
                        
                        if new_s['fast_mode'] != target_val:
                            print(f"🚀 Виконую відкладену команду: {target_val}")
                            
                            payload = {"commands": [{"code": "pd_switch_1", "value": target_val}]}
                            cmd_res = api.post(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/commands", payload)
                            
                            if cmd_res['success']:
                                mode_text = "🐢 Повільну" if target_val else "🔥 Швидку"
                    
                    # Очищаємо чергу (виконали або протухла)
                    storage.pending_cmd = None

                # --- В. ЛОГІКА СПОВІЩЕНЬ ---
                is_now_online = (new_s['in_watts'] > 5)

                if storage.was_online is None:
                    storage.was_online = is_now_online
                elif is_now_online != storage.was_online:
                    if not is_now_online:
                        storage.zero_counter += 1
                    else:
                        storage.zero_counter = 0
                    
                    if is_now_online or storage.zero_counter >= 2:
                        storage.was_online = is_now_online
                        storage.zero_counter = 0
                        if is_now_online:
                            send_telegram_bg(f"⚡ Світло Є! (+{new_s['in_watts']}W)")
                        else:
                            send_telegram_bg(f"🪫 Світло ЗНИКЛО. ({new_s['battery']}%)")

            time.sleep(1.5)
            
        except Exception as e:
            print(f"Tuya Error: {e}")
            time.sleep(5)

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
                            
                            upd_time = time.strftime("%H:%M:%S", time.localtime(storage.last_update))

                            reply = (
                                f"🔋 Статус\n"
                                f"━━━━━━━━\n"
                                f"Батарея: {s['battery']}%\n"
                                f"🟢 Вхід: {s['in_watts']} W\n"
                                f"🔌 Вихід: {s['out_watts']} W\n"
                                f"🌡 Температура: {s['temp']}°C\n"
                                f"🕒 Дані оновлено: {upd_time}"
                            )
                            send_telegram_bg(reply, target_id=cid)
            time.sleep(1)
        except:
            time.sleep(5)

@st.cache_resource
def start_threads():
    threading.Thread(target=worker_tuya, daemon=True).start()
    threading.Thread(target=worker_telegram, daemon=True).start()

start_threads()

# --- 5. ФРОНТЕНД ---

def queue_speed_command(is_slow):
    storage.pending_cmd = (is_slow, time.time())

    st.toast(f"Команду додано в чергу! Виконається при появі зв'язку (до 5хв).")

@st.fragment(run_every=1)
def monitorPage(s):
    st.markdown(f"<h1 style='text-align: center; font-size: 80px; margin-bottom: 0;'>{s['battery']}%</h1>", unsafe_allow_html=True)
    status_text = "⚡ Заряджається..." if s['is_charging'] else "🔋 Від батареї"
    
    current_time = time.time()
    last_hb = storage.last_heartbeat if storage.last_heartbeat > 0 else current_time
    last_upd = storage.last_update if storage.last_update > 0 else current_time

    ping_ago = int(current_time - last_hb)
    change_ago = int(current_time - last_upd)
    
    time_str = time.strftime("%H:%M:%S", time.localtime(last_upd))
    
    if storage.data is None:
         st.caption("⏳ Очікування даних...")
    elif ping_ago > 20:
        st.warning(f"⚠️ Втрачено зв'язок! Офлайн {ping_ago}с")
    else:
        if change_ago < 2:
            ago_text = "щойно"
        elif change_ago > 60:
            ago_text = f"{change_ago//60}хв {change_ago%60}с тому"
        elif change_ago > 3600:
            ago_text = f"{change_ago//3600}г {(change_ago%3600)//60}хв {change_ago%60}с тому"
        else:
            ago_text = f"{change_ago}с тому"
        
        # Відображаємо, якщо є команда в черзі
        if storage.pending_cmd:
            st.info("Очікує виконання команд...")
            
        st.markdown(f"<p style='text-align: center; color: gray; margin-top: -15px;'>{status_text} | {time_str} ({ago_text})</p>", unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Вхід", f"{s['in_watts']} W")
    c2.metric("Вихід", f"{s['out_watts']} W")
    
    h = s['time_left'] // 3600
    m = (s['time_left'] % 3600) // 60
    c3.metric("До кінця", f"{h}:{m:02d}")

def settingsPage(s):
    real = "Повільна" if s['fast_mode'] else "Швидка"
    
    if 'fake_val' not in st.session_state: st.session_state['fake_val'] = real
    if 'last_click' not in st.session_state: st.session_state['last_click'] = 0
    
    disp = st.session_state['fake_val'] if (time.time() - st.session_state['last_click'] < 5) else real
    
    sel = st.select_slider("Режим зарядки:", ["Повільна", "Швидка"], value=disp)
    
    if sel != disp:
        st.session_state['last_click'] = time.time()
        st.session_state['fake_val'] = sel
        
        # Замість toggle_speed_manual викликаємо queue_speed_command
        queue_speed_command(sel == "Повільна")
        st.rerun()

def main():
    s = storage.data
    
    if s is None:
        time.sleep(1)
        st.rerun()
        return

    monitor, settings = st.tabs(["Моніторинг", "Керування"])
    with monitor: monitorPage(s)
    with settings: settingsPage(s)

if __name__ == "__main__":
    main()