import os
import logging
import datetime
import asyncio
import io
import gspread
import time
import sqlite3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
import json
import threading

# --- CONFIGURATION ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 8586560620  # ✅ आपका एडमिन आईडी

# ✅ NEW: Change this to your new parent folder ID
PARENT_FOLDER_ID = "1gF_W7CGNvOrxEf2greV7UylR_b0bp3ez"  # Replace with your new folder ID

# --- QUEUE SYSTEM SETUP ---
def init_queue_db():
    """Initialize queue database"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS queue 
                 (id INTEGER PRIMARY KEY, telegram_id TEXT, position INTEGER, status TEXT, timestamp REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_users 
                 (id INTEGER PRIMARY KEY, telegram_id TEXT, start_time REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS doc_counts 
                 (id INTEGER PRIMARY KEY, telegram_id TEXT, count INTEGER, last_update REAL)''')
    conn.commit()
    conn.close()

def get_active_user_count():
    """Get count of currently active users"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM active_users")
    count = c.fetchone()[0]
    conn.close()
    return count

def add_to_active_users(telegram_id):
    """Add user to active users list"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO active_users (telegram_id, start_time) VALUES (?, ?)", 
              (telegram_id, time.time()))
    conn.commit()
    conn.close()

def remove_from_active_users(telegram_id):
    """Remove user from active users list"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("DELETE FROM active_users WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def get_doc_count(telegram_id):
    """Get document count for user"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT count FROM doc_counts WHERE telegram_id = ?", (telegram_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_doc_count(telegram_id):
    """Increment document count for user"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    current_count = get_doc_count(telegram_id)
    new_count = current_count + 1
    c.execute("INSERT OR REPLACE INTO doc_counts (telegram_id, count, last_update) VALUES (?, ?, ?)", 
              (telegram_id, new_count, time.time()))
    conn.commit()
    conn.close()
    return new_count

def reset_doc_count(telegram_id):
    """Reset document count for user"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("DELETE FROM doc_counts WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def add_to_queue(telegram_id):
    """Add user to queue"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT MAX(position) FROM queue WHERE status = 'waiting'")
    max_pos = c.fetchone()[0]
    position = (max_pos or 0) + 1
    c.execute("INSERT INTO queue (telegram_id, position, status, timestamp) VALUES (?, ?, 'waiting', ?)", 
              (telegram_id, position, time.time()))
    conn.commit()
    conn.close()
    return position

def remove_from_queue(telegram_id):
    """Remove user from queue"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("UPDATE queue SET status = 'completed' WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def get_queue_position(telegram_id):
    """Get user's position in queue"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT position FROM queue WHERE telegram_id = ? AND status = 'waiting'", (telegram_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_estimated_wait_time(position):
    """Calculate estimated wait time"""
    active_count = get_active_user_count()
    remaining_ahead = max(0, position - 50)
    estimated_minutes = remaining_ahead * 0.5  # Assuming 30 seconds per user
    return int(estimated_minutes)

def process_next_from_queue():
    """Process next user from queue"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM queue WHERE status = 'waiting' ORDER BY position LIMIT 1")
    result = c.fetchone()
    if result:
        next_user_id = result[0]
        c.execute("UPDATE queue SET status = 'completed' WHERE telegram_id = ?", (next_user_id,))
        conn.commit()
        conn.close()
        return next_user_id
    conn.close()
    return None

def cleanup_old_records():
    """Clean up old records"""
    conn = sqlite3.connect('queue.db')
    c = conn.cursor()
    
    # Clean up old active users (older than 1 hour)
    cutoff_time = time.time() - 3600
    c.execute("DELETE FROM active_users WHERE start_time < ?", (cutoff_time,))
    
    # Clean up old doc counts (older than 30 minutes)
    cutoff_time = time.time() - 1800
    c.execute("DELETE FROM doc_counts WHERE last_update < ?", (cutoff_time,))
    
    conn.commit()
    conn.close()

def queue_monitor():
    """Monitor queue and process next users"""
    while True:
        try:
            cleanup_old_records()
            
            # Check if we have space for more users
            active_count = get_active_user_count()
            if active_count < 50:
                next_user_id = process_next_from_queue()
                if next_user_id:
                    # In a real implementation, you would send a message to the user (this would require storing user references)
                    # For now, just return the user ID
                    pass
            
            time.sleep(30)  # Check every 30 seconds
        except Exception as e:
            logging.error(f"Queue monitor error: {e}")
            time.sleep(60)

# Initialize queue system
init_queue_db()
queue_thread = threading.Thread(target=queue_monitor, daemon=True)
queue_thread.start()

# --- API HELPERS (UPDATED) ---
def get_creds():
    with open('user_token.json', 'r') as f:
        token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data)
    return creds

def get_sheet():
    creds = get_creds()
    client = gspread.authorize(creds)
    # ✅ NEW: Change this to your new sheet name
    return client.open("FormCare_Data").sheet1  # Replace with your new sheet name

def create_drive_folder(name):
    creds = get_creds()
    service = build('drive', 'v3', credentials=creds)
    meta = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [PARENT_FOLDER_ID]
    }
    file = service.files().create(
        body=meta,
        fields='id, webViewLink',
        supportsAllDrives=True
    ).execute()
    return file.get('id'), file.get('webViewLink')

async def upload_to_drive(content, filename, folder_id):
    creds = get_creds()
    service = build('drive', 'v3', credentials=creds)
    
    meta = {'name': filename, 'parents': [folder_id]}
    
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype='application/octet-stream', resumable=True)
    
    service.files().create(
        body=meta,
        media_body=media,
        fields='id',
        supportsAllDrives=True
    ).execute()

# ---------- SESSION TIMEOUT CHECK ----------
def check_timeout(context):
    now = time.time()
    last_active = context.user_data.get('last_active', now)
    if (now - last_active) > (24 * 3600):  # ✅ 24 घंटे
        context.user_data.clear()
        return True
    context.user_data['last_active'] = now
    return False

# ---------- Keyboards ----------
def restart_kb():
    return ReplyKeyboardMarkup([["🔄 फिर से शुरू करें"]], resize_keyboard=True)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check if too many active users
    active_count = get_active_user_count()
    if active_count >= 50:
        # Add to queue
        position = add_to_queue(user_id)
        wait_time = get_estimated_wait_time(position)
        
        queue_msg = (
            "😊 आप फॉर्म भरने की कतार में हैं।\n"
            f"📍 आपका नंबर: {position}\n"
            f"⏰ आपकी बारी आने में: {wait_time} मिनट\n"
            "🔄 आपको अपडेट भेजे जाएंगे।"
        )
        await update.message.reply_text(queue_msg, reply_markup=restart_kb())
        return

    # Otherwise, add to active users and proceed normally
    add_to_active_users(user_id)
    
    # Check if user was in middle of process
    if context.user_data.get('waiting_docs'):
        await update.message.reply_text(
            "आप पहले से डॉक्यूमेंट भेज रहे हैं। क्या आप फिर से शुरू करना चाहते हैं?\nयदि हां, तो 'हां' लिखें।",
            reply_markup=ReplyKeyboardMarkup([["हां", "नहीं"]], resize_keyboard=True)
        )
        return

    # Otherwise, clear data and start fresh
    context.user_data.clear()
    context.user_data['last_active'] = time.time()
    kb = [["PPU 🏛️"], ["अन्य विश्वविद्यालय (Coming Soon) 🎓"], ["🔄 फिर से शुरू करें"]]
    await update.message.reply_text(
        "👋 <b>Welcome to FormCare Official Bot!</b>\n\nहम आपकी form filling प्रक्रिया को आसान और सुरक्षित बनाते हैं। ✨\n\nकृपया विश्वविद्यालय चुनें। 👇",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode="HTML"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_timeout(context):
        kb = [["🔄 फिर से शुरू करें"]]
        await update.message.reply_text(
            "⏳ 24 घंटे बीत गए हैं। फिर से शुरू करने के लिए नीचे दिए गए बटन पर क्लिक करें।",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
        return

    text = update.message.text
    if text == "🔄 फिर से शुरू करें":
        # Remove from active users if previously active
        remove_from_active_users(update.effective_user.id)
        await start(update, context)
        return

    # Handle resume after timeout
    if text.lower() == "हां":
        context.user_data.clear()
        await start(update, context)
        return
    elif text.lower() == "नहीं":
        if context.user_data.get('waiting_docs'):
            await update.message.reply_text("ठीक है। कृपया अपने दस्तावेज़ भेजें।")
        return

    # Check if waiting for student name
    if context.user_data.get('waiting_for_student_name'):
        student_name = text.strip()
        context.user_data['student_name'] = student_name
        context.user_data.pop('waiting_for_student_name', None)
        
        # Now proceed to folder creation
        phone = context.user_data.get('phone')
        name = context.user_data.get('name')  # This is the Telegram user's name (not needed here)
        
        try:
            # Google Drive Folder बनाना (फ़ोन + नाम के साथ)
            f_id, f_link = create_drive_folder(f"{phone}_{student_name}")
            context.user_data['f_id'] = f_id
            
            # Save user's chat_id
            context.user_data['chat_id'] = update.effective_chat.id  # ✅ यूजर का chat_id सेव करें
            # Save phone and chat_id mapping in a JSON file
            save_user_mapping(phone, update.effective_chat.id)  # ✅ नया फ़ंक्शन
            
            # Google Sheet में Data डालना (सही क्रम में)
            sheet = get_sheet()
            sheet.append_row([
                student_name,  # ✅ यहाँ यूजर द्वारा टाइप किया गया नाम जाएगा
                phone, 
                context.user_data.get('univ'), 
                context.user_data.get('college'), 
                context.user_data.get('course'), 
                context.user_data.get('session'), 
                context.user_data.get('semester_context', ''),  # ✅ Semester
                f_link,  # ✅ Folder Link
                "Pending ⏳"  # ✅ Status
            ])
            
            # --- NEW UPDATED MESSAGE LOGIC ---
            if context.user_data.get('session') == "2025–29" and "Semester 2" in context.user_data.get('final_selection', ''):
                msg = (f"✅ मोबाइल नंबर: <b>{phone}</b>\n\n"
                       f"📜 <b>नामांकन के लिए आवश्यक कागजात (Semester-II):</b>\n"
                       f"1️⃣ Semester-I का नामांकन रसीद का छाया प्रति।\n"
                       f"2️⃣ एक फोटो। 🤳\n"
                       f"3️⃣ U.G Semester-I Admit Card 📄\n"
                       f"4️⃣ BC-I, SC & ST जाति प्रमाण पत्र। 📂\n"
                       f"5️⃣ Aadhar Card 🆔\n\n"
                       f"एक-एक करके फोटो या फाइल भेजें।")
            else:
                msg = f"✅ वेरिफिकेशन सफल! <b>{phone}</b>\n\nकृपया अपने दस्तावेज़ (Documents) भेजना शुरू करें। 📁"

            await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
            context.user_data['waiting_docs'] = True
            context.user_data['doc_count'] = 0
            context.user_data['last_active'] = time.time()
        except Exception as e:
            logging.error(f"❌ CRITICAL ERROR in Contact Handler: {e}")
            print(f"❌ DETAILED ERROR: {e}") 
            await update.message.reply_text("⚠️ सिस्टम एरर! कृपया एडमिन से संपर्क करें।")
        return

    # --- GLOBAL COMING SOON CHECK ---
    if any(x in text for x in ["Coming Soon", "🔒", "Completed", "✍️", "📝"]) and "Upcoming" not in text:
        await update.message.reply_text(
            "⏳ यह विकल्प अभी उपलब्ध नहीं है।\nयह सुविधा भविष्य में सक्रिय की जाएगी। 🔜", 
            reply_markup=restart_kb(),
            parse_mode="HTML"
        )
        return
    
    if "Upcoming" in text:
        await update.message.reply_text(
            "📢 <b>Coming Soon!</b>\nयह फॉर्म जल्द ही शुरू होने वाला है। कृपया अपडेट के लिए जुड़े रहें। 🔜",
            reply_markup=restart_kb(),
            parse_mode="HTML"
        )
        return

    # 1. PPU & College
    if text == "PPU 🏛️":
        context.user_data['univ'] = "PPU"
        kb = [["MD College Naubatpur 🏫"], ["अन्य कॉलेज (Coming Soon) 🏢"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("आपने PPU विश्वविद्यालय चुना है। ✅\n\nकृपया कॉलेज चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text == "MD College Naubatpur 🏫":
        context.user_data['college'] = "MD College Naubatpur"
        kb = [["Intermediate (इंटरमीडिएट) 🎒"], ["UG (स्नातक) 🎓"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("MD College Naubatpur चुना गया। 📍\n\nकृपया अपना course चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    # 2. INTERMEDIATE FLOW
    if text == "Intermediate (इंटरमीडिएट) 🎒":
        context.user_data['course'] = "Intermediate"
        kb = [["Science (विज्ञान) 🧪"], ["Arts (कला) 🎨"], ["Commerce (वाणिज्य) 📊"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("कृपया अपनी <b>stream (संकाय)</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text in ["Science (विज्ञान) 🧪", "Arts (कला) 🎨", "Commerce (वाणिज्य) 📊"]:
        context.user_data['stream'] = text
        kb = [["2025–27 📅"], ["2026–28 📅"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("कृपया <b>session</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text in ["2025–27 📅", "2026–28 📅"]:
        context.user_data['session'] = text
        kb = [["11वीं 📚"], ["12वीं 📚"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("कृपया <b>class</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text in ["11वीं 📚", "12वीं 📚"]:
        context.user_data['class'] = text
        kb = [["Admission Form (प्रवेश प्रपत्र) 📝"], ["Examination Form (परीक्षा प्रपत्र) ✍️"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("कृपया <b>form type</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    # 3. UG FLOW
    if text == "UG (स्नातक) 🎓":
        context.user_data['course'] = "UG"
        kb = [["2023–27"], ["2024–28"], ["2025–29"], ["2026–30"], ["🔄 फिर से शुरू करें"]]
        await update.message.reply_text("कृपया <b>session</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text in ["2023–27", "2024–28", "2025–29", "2026–30"]:
        context.user_data['session'] = text
        await ug_semesters(update, text)
        return

    if text == "Semester 2 🟢 LIVE":
        context.user_data['semester_context'] = "Semester 2"
        kb = [
            ["Admission Form (प्रवेश प्रपत्र) 🟢 LIVE"], 
            ["Examination Form (परीक्षा प्रपत्र) (Upcoming) 🔜"], 
            ["🔄 फिर से शुरू करें"]
        ]
        await update.message.reply_text("कृपया <b>Form Type</b> चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")
        return

    if text == "Admission Form (प्रवेश प्रपत्र) 🟢 LIVE":
        context.user_data['final_selection'] = "Semester 2 Admission Form"
        await request_mobile(update)
        return

    if "🟢 LIVE" in text and text != "Admission Form (प्रवेश प्रपत्र) 🟢 LIVE": 
        context.user_data['final_selection'] = text
        await request_mobile(update)
        return

async def ug_semesters(update: Update, session: str):
    sems = []
    if session == "2023–27":
        sems = [["Sem 1-5 ✅ Completed"], ["Semester 6 (Upcoming) 🔜"], ["Sem 7-8 (Coming Soon) 🔒"]]
    elif session == "2024–28":
        sems = [["Sem 1-3 ✅ Completed"], ["Semester 4 (Upcoming) 🔜"], ["Sem 5-8 (Coming Soon) 🔒"]]
    elif session == "2025–29":
        sems = [["Semester 1 ✅ Completed"], ["Semester 2 🟢 LIVE"], ["Sem 3-8 (Coming Soon) 🔒"]]
    else:
        sems = [["सभी सेमेस्टर (Coming Soon) 🔒"]]
    
    kb = sems + [["🔄 फिर से शुरू करें"]]
    await update.message.reply_text(f"🎓 <b>UG Session {session}</b>\n\nकृपया अपना semester चुनें। 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="HTML")

async def request_mobile(update: Update):
    btn = [[KeyboardButton("📱 अपना मोबाइल नंबर साझा करें", request_contact=True)]]
    await update.message.reply_text("🔒 <b>वेरिफिकेशन स्टेप</b>\n\n⚠️ <b>ध्यान दें:</b> नंबर साझा करने के लिए नीचे दिए गए बटन का उपयोग करें। 👇", reply_markup=ReplyKeyboardMarkup(btn, resize_keyboard=True, one_time_keyboard=True), parse_mode="HTML")

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    phone = contact.phone_number
    name = f"{update.message.from_user.first_name} {update.message.from_user.last_name or ''}".strip()
    
    context.user_data['phone'] = phone
    context.user_data['name'] = name
    
    # Now ask for student name
    await update.message.reply_text(
        "📝 कृपया अपना नाम बताएं।",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data['waiting_for_student_name'] = True

async def handle_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_timeout(context):
        kb = [["🔄 फिर से शुरू करें"]]
        await update.message.reply_text(
            "⏳ 24 घंटे बीत गए हैं। फिर से शुरू करने के लिए नीचे दिए गए बटन पर क्लिक करें।",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
        return

    if context.user_data.get('waiting_docs'):
        f_id = context.user_data.get('f_id')
        if not f_id:
            await update.message.reply_text("⚠️ सेशन एक्सपायर! /start करें।")
            return
        try:
            # File Handling
            file_item = update.message.photo[-1] if update.message.photo else update.message.document
            file = await context.bot.get_file(file_item.file_id)
            
            # Download file to memory
            out_buffer = io.BytesIO()
            await file.download_to_memory(out_buffer)
            out_buffer.seek(0) # Reset buffer
            
            ext = ".jpg" if update.message.photo else ".pdf"
            fname = f"Doc_{datetime.datetime.now().strftime('%H%M%S')}{ext}"
            
            # Upload content
            await upload_to_drive(out_buffer.read(), fname, f_id)
            
            # Forward to Admin
            await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            
            # ✅ नया मैसेज (Document Counter के साथ)
            doc_count = context.user_data.get('doc_count', 0) + 1
            context.user_data['doc_count'] = doc_count  # Count Update
            
            # Increment document count for queue system
            increment_doc_count(update.effective_user.id)
            
            await update.message.reply_text(
                f"✅ {doc_count} document प्राप्त हुआ।\n\n"
                f"अब हमारी टीम आपके documents को verify करेगी और verification के बाद आपको सूचित किया जाएगा।"
            )
            
            # Check for 20-second timeout to mark as finished
            # In a real implementation, this would be handled by the queue monitor
            # For now, we'll just increment the counter
            
        except Exception as e:
            logging.error(f"Error in docs: {e}")
            print(f"❌ DOC UPLOAD ERROR: {e}")
            await update.message.reply_text("⚠️ फाइल अपलोड में समस्या आई।")

# ✅ फ़ोन और chat_id को JSON में सेव करने का फ़ंक्शन
def save_user_mapping(phone, chat_id):
    try:
        with open('user_mapping.json', 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    
    data[phone] = chat_id
    
    with open('user_mapping.json', 'w') as f:
        json.dump(data, f)

# ✅ एडमिन द्वारा वेरिफाई करने के लिए नया फ़ंक्शन (फ़ोन + नाम दोनों से ढूंढेगा)
async def verify_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ आपको यह करने की अनुमति नहीं है।")
        return

    try:
        phone_number = context.args[0]
        student_name = " ".join(context.args[1:])  # नाम को अलग करें
        sheet = get_sheet()
        all_values = sheet.get_all_values()
        
        row_index = None
        for i, row in enumerate(all_values):
            if len(row) > 1 and row[1] == phone_number and student_name.lower() in row[0].lower():  # Name column is index 0
                row_index = i + 1  # Google Sheets rows are 1-indexed
                break

        if row_index:
            # Update status to Verified in Column I (index 8)
            sheet.update_cell(row_index, 9, "Verified ✅")  # Column I = 9
            
            # Get user's chat_id from saved data
            try:
                with open('user_mapping.json', 'r') as f:
                    user_data = json.load(f)
                user_chat_id = user_data.get(phone_number)
            except FileNotFoundError:
                user_chat_id = None

            if user_chat_id:
                # Send message to user
                await context.bot.send_message(
                    chat_id=user_chat_id,
                    text=f"✅ <b>Documents Verification Completed</b>\nआपके दस्तावेज़ों का सत्यापन सफलतापूर्वक पूरा हो गया है।\nकोई भी त्रुटि नहीं पाई गई है।\n\n<b>🔜 अगला स्टेप:</b> पेमेंट करें।\nपेमेंट केवल ऑफिशियल FormCare WhatsApp नंबर पर ही करें:\n📱 <b>9234992071</b>\n\n⚠️ <b>सावधान:</b> कोई भी अन्य नंबर या व्यक्ति आपसे पेमेंट नहीं लेगा।\nऐसे किसी भी व्यक्ति से सावधान रहें जो खुद को FormCare स्टाफ़ बताकर पेमेंट करने को कहे।\nहमारा एकमात्र ऑफिशियल WhatsApp: <b>9234992071</b>",
                    parse_mode="HTML"
                )
                await update.message.reply_text(f"✅ {phone_number} - {student_name} को वेरिफिकेशन मैसेज भेज दिया गया और स्प्रेडशीट में Verified कर दिया गया।")
                
                # Remove from active users since verification is complete
                remove_from_active_users(int(user_chat_id))
            else:
                await update.message.reply_text(f"❌ यूजर का chat_id नहीं मिला। कृपया यूजर को फिर से बॉट से संपर्क करने को कहें।")
        else:
            await update.message.reply_text(f"❌ फ़ोन नंबर {phone_number} और नाम {student_name} स्प्रेडशीट में नहीं मिला।")
    except Exception as e:
        await update.message.reply_text(f"❌ वेरिफाई करने में त्रुटि: {e}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("restart", start))
    app.add_handler(CommandHandler("home", start))
    app.add_handler(CommandHandler("verify", verify_user))  # ✅ नया कमांड
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_docs))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()