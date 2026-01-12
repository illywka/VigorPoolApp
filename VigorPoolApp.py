import streamlit as st
from tuya_connector import TuyaOpenAPI
import base64
import struct
import time
import requests
import threading

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
        
        # --- WATCHDOG: ПАМ'ЯТЬ ДЛЯ ЗАЛИПАННЯ ---
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

    c_data = next((i['value'] for i in api_result if i['code'] == 'charged_data'), None)
    if c_data:
        try:
            raw = base64.b64decode(c_data)
            p_in, t_full = struct.unpack('<ii', raw[:8])
                
        except: pass

    d_data = next((i['value'] for i in api_result if i['code'] == 'battery_parameters'), None)
    if d_data:
        try:
            raw = base64.b64decode(d_data)
            p_out, _, t_empty = struct.unpack('<iii', raw[:12])
            if new_s['in_watts'] > 0 and new_s['in_watts'] > new_s['out_watts']:
                s['time_left'] = t_full
            else:
                s['time_left'] = t_empty
        except: pass
    
    # Додатковий захист
    if s['battery'] == 100: s['in_watts'] = 0
        
    return s

def send_telegram_bg(message, target_id=None):
    try:
        token = st.secrets["BOT_TOKEN"]
        chat_id = target_id if target_id else st.secrets["CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      data={"chat_id": chat_id, "text": message}, timeout=5)
    except: pass

def worker_tuya():
    api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
    current_sleep_time = 60
    while True:
        try:
            if not api.is_connect():
                api.connect()
            
            res = api.get(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/status")
            if res['success']:
                new_s = get_vigor_state(res['result'])
                curr_time = time.time() + 7200

                
                # 1. Перевірка ВХОДУ
                if new_s['in_watts'] != storage.last_in_val:
                    # Значення змінилось - все ок, оновлюємо таймер
                    storage.last_in_val = new_s['in_watts']
                    storage.last_in_change = curr_time
                elif new_s['in_watts'] > 0 and (curr_time - storage.last_in_change) > 300:
                    # Значення висить > 300 сек -> Скидаємо в 0
                    new_s['in_watts'] = 0
                    new_s['is_charging'] = False
                
                # 2. Перевірка ВИХОДУ
                if new_s['out_watts'] != storage.last_out_val:
                    storage.last_out_val = new_s['out_watts']
                    storage.last_out_change = curr_time
                elif new_s['out_watts'] > 0 and (curr_time - storage.last_out_change) > 120:
                    # Значення висить > 60 сек -> Скидаємо в 0
                    new_s['out_watts'] = 0
                
                new_s['out_watts'] = p_out
                new_s['in_watts'] = p_in
                if new_s['in_watts'] > 0 and new_s['in_watts'] > new_s['out_watts']:
                    s['time_left'] = t_full
                else:
                    s['time_left'] = t_empty
                    
                # ============================================

                if storage.data is None or storage.data != new_s:
                    storage.data = new_s
                    storage.last_update = curr_time
                
                # Команди
                if storage.pending_cmd:
                    target_val, created_at = storage.pending_cmd
                    if (curr_time - created_at) < 300:
                        if new_s['fast_mode'] != target_val:
                            pl = {"commands": [{"code": "pd_switch_1", "value": target_val}]}
                            if api.post(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/commands", pl)['success']:
                                storage.pending_cmd = None
                    else: storage.pending_cmd = None

                elif new_s['in_watts'] > 0 or new_s['out_watts'] > 0:
                    current_sleep_time = 20
                else:
                    current_sleep_time = 120

                is_fresh = (curr_time - storage.last_update) < (current_sleep_time + 20)
                has_power = (new_s['in_watts'] > 405)
                is_now_online = has_power and is_fresh
                
                if storage.was_online is None:
                    storage.was_online = is_now_online
                elif is_now_online != storage.was_online:
                    if not is_now_online: storage.zero_counter += 1
                    else: storage.zero_counter = 0
                    
                    if is_now_online or storage.zero_counter >= 2:
                        storage.was_online = is_now_online
                        storage.zero_counter = 0
                        if is_now_online:
                            send_telegram_bg(f"Світло Є!")
                        else:
                            send_telegram_bg(f"Зарядка закінчилась. ({new_s['battery']}%)")
                storage.history.append({
                    "time": time.strftime("%H:%M:%S", time.localtime(curr_time)),
                    "Вхід (W)": new_s['in_watts'],
                    "Вихід (W)": new_s['out_watts']
                })
                # Обмежуємо довжину (останні 100 записів)
                if len(storage.history) > 100:
                    storage.history.pop(0)
            else:
                print("Station is now offline. Sleeping 5 min.")
                current_sleep_time = 300

            time.sleep(current_sleep_time)
            
        except Exception as e:
            time.sleep(30)

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

@st.cache_resource
def start_threads():
    threading.Thread(target=worker_tuya, daemon=True).start()
    threading.Thread(target=worker_telegram, daemon=True).start()

start_threads()

# --- 5. ФРОНТЕНД ---

def queue_speed_command(is_slow):
    storage.pending_cmd = (is_slow, time.time())
    st.toast(f"Команду додано в чергу!", icon="⏳")

@st.fragment(run_every=1)
def monitorPage():
    s = storage.data
    if s is None:
        s = { "battery": "--", "temp": 0, "in_watts": 0, "out_watts": 0, "time_left": 0, "is_charging": False, "fast_mode": False }
        is_loading = True
    else:
        is_loading = False

    st.markdown(f"<h1 style='text-align: center; font-size: 80px; margin-bottom: 0;'>{s['battery']}%</h1>", unsafe_allow_html=True)
    
    if is_loading:
        st.markdown(f"<p style='text-align: center; color: gray;'>📡 Підключення...</p>", unsafe_allow_html=True)
        display_in, display_out, display_time = 0, 0, "--:--"
    else:
        status_text = "⚡ Заряджається..." if s['is_charging'] else "🔋 Від батареї"
        curr = time.time() + 7200
        change_ago = int(curr - storage.last_update) if storage.last_update else 0
        time_str = time.strftime("%H:%M:%S", time.localtime(storage.last_update)) if storage.last_update else "--:--"
        
        if change_ago < 2:
            ago_text = "щойно" 
        elif change_ago < 60: 
            ago_text = f"{change_ago}с тому"
        elif change_ago < 3600:
            ago_text = f"{change_ago // 60}хв {change_ago%60}с тому"
        else:
            ago_text = f"{s['time_left'] // 3600}г {change_ago // 60}хв {change_ago%60}с тому"
        if storage.pending_cmd: st.info("Очікує виконання команд...")
        st.markdown(f"<p style='text-align: center; color: gray; margin-top: -15px;'>{status_text} | {time_str} ({ago_text})</p>", unsafe_allow_html=True)

        display_in, display_out = s['in_watts'], s['out_watts']
        h, m = s['time_left'] // 3600, (s['time_left'] % 3600) // 60
        display_time = f"{h}г {m:02d}хв"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Вхід", f"{display_in} W")
    c2.metric("Вихід", f"{display_out} W")
    c3.metric("До кінця", display_time)
    c4.metric("Темп.", f"{s['temp']}°C")

        # Вивід графіка з налаштованими кольорами
    if storage.history:
        chart_colors = {
            "Вхід (W)": "#00c853", 
            "Вихід (W)": "#ff4b4b"
        }
        
        st.line_chart(
            storage.history, 
            x="time", 
            y=["Вхід (W)", "Вихід (W)"], 
            color=["#00c853", "#ff4b4b"] # Список кольорів по черзі для кожної лінії
        )
        

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