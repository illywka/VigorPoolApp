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
        self.last_update = 0    # Час останньої ЗМІНИ цифр
        self.last_heartbeat = 0 # Час останнього КОНТАКТУ з сервером
        self.telegram_offset = 0
        self.was_online = None 
        self.zero_counter = 0   

storage = SharedStorage()

# --- 2. ДОПОМІЖНІ ФУНКЦІЇ ---

def get_vigor_state(api_result):
    s = { "battery": 0, "temp": 0, "in_watts": 0, "out_watts": 0, "time_left": 0, "is_charging": False, "fast_mode": False }
    s['battery'] = next((i['value'] for i in api_result if i['code'] == 'battery_percentage'), 0)
    s['temp'] = next((i['value'] for i in api_result if i['code'] == 'temp_current'), 0)
    s['fast_mode'] = next((i['value'] for i in api_result if i['code'] == 'pd_switch_1'), False) # True=Slow, False=Fast

    c_data = next((i['value'] for i in api_result if i['code'] == 'charged_data'), None)
    if c_data == "yAAAAFYAAAA=": 
            s['in_watts'] = 0
            s['is_charging'] = False
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
            p_out, _, t_empty = struct.unpack('<iii', base64.b64decode(d_data))
            s['out_watts'] = p_out
            if not s['is_charging']:
                s['time_left'] = t_empty
        except: pass
    return s

# Глобальна змінна для зберігання часу останньої відправки
last_sent_time = 0

def send_telegram(message):
    try:
        token = st.secrets["BOT_TOKEN"]
        chat_id = st.secrets["CHAT_ID"]
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      data={"chat_id": chat_id, "text": message}, timeout=5)
    except Exception as e:
        print(f"Telegram Error: {e}")


# --- 3. ПОТІК 1: TUYA (Станція) ---
def worker_tuya():
    while True:
        try:
            api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
            api.connect()
            
            res = api.get(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/status")
            
            if res['success']:
                new_s = get_vigor_state(res['result'])
                
                # --- ЛОГІКА: ОНОВЛЕННЯ ТІЛЬКИ ПРИ ЗМІНАХ ---
                
                # 1. Завжди оновлюємо "Пульс" (ми бачимо станцію, вона онлайн)
                storage.last_heartbeat = time.time()
                
                # 2. Перевіряємо, чи змінилися дані
                # Якщо це перший запуск АБО дані відрізняються
                if storage.data is None or storage.data != new_s:
                    storage.data = new_s
                    storage.last_update = time.time() # Оновлюємо час зміни

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
                            send_telegram(f"⚡ Світло Є! (+{new_s['in_watts']}W)")
                        else:
                            send_telegram(f"Батарея: {new_s['battery']}%")

            time.sleep(1.5)
            
        except Exception as e:
            print(f"Tuya Error: {e}")
            time.sleep(5)

def worker_telegram():
    prev_power = None
    while True:
        try:
            api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
            api.connect()
            res = api.get(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/status")
            if res['success']:
                new_s = get_vigor_state(res['result'])
                storage.data = new_s

                if prev_power is not None:
                    if prev_power < 5 and new_s['in_watts'] >= 5:
                        send_telegram("⚡ Світло є!")

                    if prev_power >= 5 and new_s['in_watts'] < 5:
                        send_telegram(f"Зарядка закінчилась ({new_s['battery']}%)")

                prev_power = new_s['in_watts']
            # Якщо даних про станцію ще немає - чекаємо
            if storage.data is None:
                time.sleep(2)
                continue

            token = st.secrets["BOT_TOKEN"]
            chat_id = str(st.secrets["CHAT_ID"])
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            
            params = {"offset": storage.telegram_offset + 1, "timeout": 10}
            
            try:
                resp = requests.get(url, params=params, timeout=15).json()
            except: 
                time.sleep(1)
                continue

            if resp.get('ok') and resp.get('result'):
                for update in resp['result']:
                    storage.telegram_offset = update['update_id']
                    
                    text = update.get('message', {}).get('text', '').lower()
                    cid = str(update.get('message', {}).get('chat', {}).get('id', ''))
                    
                    if cid == chat_id:
                        if "/status" in text or "статус" in text:
                            s = storage.data # Беремо актуальні дані
                            h = s['time_left'] // 3600
                            m = (s['time_left'] % 3600) // 60
                            reply = (
                                f"🔋 Статус\nБатарея: {s['battery']}%\n"
                                f"Вхід: {s['in_watts']}W | Вихід: {s['out_watts']}W\n"
                                f"Орієнтовний час: {h}г {m:02d}хв\n"
                                f"Температура: {s['temp']}℃"
                            )
                            send_telegram(reply)
            
            # Маленька пауза, щоб не спамити API
            time.sleep(2)

        except Exception as e:
            print(f"Telegram Error: {e}")
            time.sleep(5)

# --- ЗАПУСК ПОТОКІВ ---
@st.cache_resource
def start_threads():
    t1 = threading.Thread(target=worker_tuya, daemon=True)
    t2 = threading.Thread(target=worker_telegram, daemon=True)
    t1.start()
    t2.start()
    return t1, t2

start_threads()


# --- 5. ФРОНТЕНД (Миттєвий) ---

def toggle_speed_manual(is_slow):
    try:
        api = TuyaOpenAPI("https://openapi.tuyaeu.com", st.secrets["ACCESS_ID"], st.secrets["ACCESS_KEY"])
        api.connect()
        payload = {"commands": [{"code": "pd_switch_1", "value": is_slow}]}
        api.post(f"/v1.0/devices/{st.secrets['DEVICE_ID']}/commands", payload)
    except: pass

def monitorPage(s):
    # Візуалізація
    st.markdown(f"<h1 style='text-align: center; font-size: 80px; margin-bottom: 0;'>{s['battery']}%</h1>", unsafe_allow_html=True)

    status_text = "⚡ Заряджається..." if s['is_charging'] else "🔋 Від батареї"
    
    # --- НОВА ЛОГІКА ЧАСУ ---
    current_time = time.time()
    
    ping_ago = int(current_time - storage.last_heartbeat)

    change_ago = int(current_time - storage.last_update)
    
    if storage.last_heartbeat == 0:
        st.caption("Очікування першого підключення...")
    elif ping_ago > 20:
        # Червона тривога: немає зв'язку
        st.warning(f"⚠️ Втрачено зв'язок! Офлайн {ping_ago}с")
    else:
        time_str = time.strftime("%H:%M:%S", time.localtime(storage.last_update))
        
        # Красивий підпис
        if change_ago < 2:
            ago_text = "щойно"
        elif change_ago > 60:
            ago_text = f"{change_ago//60}хв {change_ago%60}с тому"
        elif change_ago > 3600:
            ago_text = f"{change_ago//3600}г {(change_ago%3600)//60}хв {change_ago%60}с тому"
        else:
            ago_text = f"{change_ago}с тому"
            
        st.markdown(
            f"<p style='text-align: center; color: gray; margin-top: -15px;'>"
            f"{status_text} | Дані оновлено: {time_str} ({ago_text})</p>", 
            unsafe_allow_html=True
        )
    # -----------------------

    c1, c2, c3 = st.columns(3)
    c1.metric("Вхід", f"{s['in_watts']} W")
    c2.metric("Вихід", f"{s['out_watts']} W")
    
    h = s['time_left'] // 3600
    m = (s['time_left'] % 3600) // 60
    c3.metric("До кінця", f"{h}г {m:02d}хв")

def settingsPage(s):
    real_label = "Повільна" if s['fast_mode'] else "Швидка"
    
    if 'fake_val' not in st.session_state: st.session_state['fake_val'] = real_label
    if 'last_click' not in st.session_state: st.session_state['last_click'] = 0
    
    disp = st.session_state['fake_val'] if (time.time() - st.session_state['last_click'] < 5) else real_label
    
    sel = st.select_slider("Режим зарядки:", ["Повільна", "Швидка"], value=disp)
    
    if sel != disp:
        st.session_state['last_click'] = time.time()
        st.session_state['fake_val'] = sel
        
        should_be_slow = (sel == "Повільна")
        toggle_speed_manual(should_be_slow)
        
        st.toast(f"Перемикаю на: {sel}")
        time.sleep(0.1)
        st.rerun()

def main():
    s = storage.data
    
    if s is None:
        time.sleep(1)
        st.rerun()
        return

    ping = time.time() - storage.last_update
    if ping > 10:
        st.warning(f"⚠️ Дані застаріли ({int(ping)}с). Перевірте зв'язок станції з Wi-Fi.")

    monitor, settings = st.tabs(["Моніторинг", "Керування"])
    
    with monitor:
        monitorPage(s)
    with settings:
        settingsPage(s)

    time.sleep(1)
    st.rerun()

if __name__ == "__main__":
    main()