import os
import html
import time
import threading
import re
from datetime import datetime, timedelta
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from openpyxl import load_workbook
from bot.session_manager import SessionManager
from bot.bot_utils import (
    search_address,
    get_shipping_estimates,
    format_results_message,
    create_number_buttons,
    create_detail_buttons,
    create_back_button,
    ITEMS_PER_PAGE
)

# Konfigurasi
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# Inisialisasi komponen
session_manager = SessionManager()
user_states = {}
user_states_lock = threading.Lock()
bot = None

def start_bot(token):
    global bot
    bot = TeleBot(token)

    @bot.message_handler(commands=['start'])
    def send_welcome(message):
        text = (
            "🇮🇩 BOT PENCARIAN ALAMAT INDONESIA\n"
            "Ketik nama wilayah yang ingin dicari:\n"
            "Contoh: Bakongan atau 23773\n"
            "Gunakan filter spesifik:\n"
            "kelurahan:Bakongan provinsi:Aceh\n"
            "Atau kombinasi teks bebas dan filter:\n"
            "Bakongan provinsi:Aceh"
        )
        bot.send_message(message.chat.id, text, parse_mode='HTML')

    @bot.message_handler(func=lambda m: True)
    def handle_search(message):
        user_id = message.from_user.id
        query = message.text.strip()

        with user_states_lock:
            state = user_states.get(user_id)

            if state == "waiting_for_name":
                if len(query) < 3:
                    bot.reply_to(message, "❌ Nama minimal 3 karakter")
                    return
                user_states[user_id] = {"name": query, "state": "waiting_for_phone"}
                bot.reply_to(message, "📱 Masukkan nomor HP penerima:")
                return

            elif isinstance(state, dict) and state.get("state") == "waiting_for_phone":
                if not query.isdigit() or len(query) < 10:
                    bot.reply_to(message, "❌ Nomor HP tidak valid")
                    return
                user_states[user_id]["phone"] = query
                user_states[user_id]["state"] = "waiting_for_address"
                bot.reply_to(message, "🏠 Masukkan alamat lengkap:")
                return

            elif isinstance(state, dict) and state.get("state") == "waiting_for_address":
                if len(query) < 10:
                    bot.reply_to(message, "❌ Alamat terlalu pendek")
                    return
                user_states[user_id]["address"] = query
                user_states[user_id]["state"] = "waiting_for_courier"
                bot.send_message(message.chat.id, "🚚 Pilih jasa kirim:", reply_markup=create_courier_buttons(user_id))
                return

        results = search_address(query)
        if not results:
            bot.reply_to(message, "❌ Tidak ditemukan hasil untuk pencarian tersebut")
            return

        session_manager.save_results(user_id, results)
        msg_content = format_results_message(results, 1)
        markup = create_number_buttons(results, 1, user_id)
        bot.send_message(
            message.chat.id,
            f"🔍 Ditemukan {len(results)} hasil:\n{msg_content}",
            reply_markup=markup,
            parse_mode='HTML'
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith('COURIER_'))
    def handle_courier_selection(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        courier = call.data.split('_')[2]

        with user_states_lock:
            if user_id not in user_states or "state" not in user_states[user_id]:
                bot.answer_callback_query(call.id, "Sesi telah berakhir")
                return

            user_states[user_id]["courier"] = courier
            user_states[user_id]["state"] = "waiting_for_cod"

        markup = create_cod_buttons(user_id)
        bot.send_message(call.message.chat.id, "COD Ongkir:", reply_markup=markup)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('COD_'))
    def handle_cod_selection(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        cod_option = call.data.split('_')[2]

        with user_states_lock:
            if user_id not in user_states or "state" not in user_states[user_id]:
                bot.answer_callback_query(call.id, "Sesi telah berakhir")
                return

            user_states[user_id]["cod"] = cod_option
            process_cetak_resi(call.message.chat.id, user_id)

        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('HALAMAN_'))
    def handle_page(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        _, user_id, page = call.data.split('_')
        user_id = int(user_id)
        page = int(page)
        results = session_manager.get_results(user_id)
        if not results:
            bot.answer_callback_query(call.id, "Sesi telah berakhir")
            return

        msg_content = format_results_message(results, page)
        markup = create_number_buttons(results, page, user_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔍 Ditemukan {len(results)} hasil:\n{msg_content}",
            reply_markup=markup,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('PILIH_'))
    def handle_selection(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        _, user_id, idx = call.data.split('_')
        user_id = int(user_id)
        idx = int(idx)
        results = session_manager.get_results(user_id)
        if not results or idx >= len(results):
            bot.answer_callback_query(call.id, "Data tidak tersedia")
            return

        selected = results[idx]
        session_manager.save_selected_address(user_id, selected)
        detail = (
            "🔍 DETAIL LENGKAP\n"
            f"🏘️ Kelurahan: {html.escape(selected['kelurahan'])}\n"
            f"📍 Kecamatan: {html.escape(selected['kecamatan'])}\n"
            f"🏙️ Kota/Kab: {html.escape(selected['kota'])}\n"
            f"🌏 Provinsi: {html.escape(selected['provinsi'])}\n"
            f"📮 Kode Pos: {selected['kode_pos']}\n"
            f"🔑 Kode Kemendagri: {html.escape(selected['kode_kemendagri'])}"
        )
        markup = create_detail_buttons(user_id)
        bot.send_message(call.message.chat.id, detail, reply_markup=markup, parse_mode='HTML')
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('CEKONGKIR_'))
    def handle_cek_ongkir(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        selected_address = session_manager.get_selected_address(user_id)
        if not selected_address:
            bot.answer_callback_query(call.id, "Alamat tidak tersedia")
            return

        postal_code = selected_address['kode_pos']
        estimates = get_shipping_estimates(postal_code)
        if isinstance(estimates, str):
            response_text = estimates
        else:
            response_text = "🚚 ESTIMASI BIAYA PENGIRIMAN\n"
            for courier_name, courier_info in estimates.items():
                price = courier_info.get("price", "Tidak diketahui")
                estimate_delivery = courier_info.get("estimate_delivery", "Tidak diketahui")
                response_text += (
                    f"🚚 Kurir: {courier_name}\n"
                    f"💰 Harga: Rp {price}\n"
                    f"⏱️ Estimasi Pengiriman: {estimate_delivery}\n\n"
                )

        markup = create_back_button(user_id)
        bot.send_message(call.message.chat.id, response_text, reply_markup=markup, parse_mode='HTML')
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('BACK_'))
    def handle_back(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        results = session_manager.get_results(user_id)
        if not results:
            bot.answer_callback_query(call.id, "Sesi telah berakhir")
            return

        msg_content = format_results_message(results, 1)
        markup = create_number_buttons(results, 1, user_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🔍 Ditemukan {len(results)} hasil:\n{msg_content}",
            reply_markup=markup,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('BACKDETAIL_'))
    def handle_back_detail(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        selected_address = session_manager.get_selected_address(user_id)
        if not selected_address:
            bot.answer_callback_query(call.id, "Alamat tidak tersedia")
            return

        detail = (
            "🔍 DETAIL LENGKAP\n"
            f"🏘️ Kelurahan: {html.escape(selected_address['kelurahan'])}\n"
            f"📍 Kecamatan: {html.escape(selected_address['kecamatan'])}\n"
            f"🏙️ Kota/Kab: {html.escape(selected_address['kota'])}\n"
            f"🌏 Provinsi: {html.escape(selected_address['provinsi'])}\n"
            f"📮 Kode Pos: {selected_address['kode_pos']}\n"
            f"🔑 Kode Kemendagri: {html.escape(selected_address['kode_kemendagri'])}"
        )
        markup = create_detail_buttons(user_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=detail,
            reply_markup=markup,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith('CETAKRESI_'))
    def handle_cetak_resi(call):
        if not validate_user(call):
            bot.answer_callback_query(call.id, "Unauthorized access")
            return

        user_id = int(call.data.split('_')[1])
        with user_states_lock:
            user_states[user_id] = "waiting_for_name"
        bot.send_message(call.message.chat.id, "📱 Masukkan nama penerima:")
        bot.answer_callback_query(call.id)

    def validate_user(call):
        try:
            expected_user = int(call.data.split('_')[1])
            return call.from_user.id == expected_user
        except:
            return False

    def process_cetak_resi(chat_id, user_id):
        with user_states_lock:
            user_data = user_states.get(user_id)
            if not user_data:
                bot.send_message(chat_id, "❌ Data tidak tersedia")
                return

        selected_address = session_manager.get_selected_address(user_id)
        if not selected_address:
            bot.send_message(chat_id, "❌ Alamat tidak tersedia")
            return

        try:
            safe_name = sanitize_filename(user_data['name'])
            timestamp = int(time.time())
            output_path = f"data/resi_{safe_name}_{timestamp}.xlsx"

            if not os.path.exists("data/label.xlsx"):
                bot.send_message(chat_id, "❌ Template resi tidak ditemukan")
                return

            workbook = load_workbook("data/label.xlsx")
            sheet = workbook.active

            full_address = (
                f"{user_data['address']}, "
                f"{selected_address['kelurahan']}, "
                f"{selected_address['kecamatan']}, "
                f"{selected_address['kota']}, "
                f"{selected_address['provinsi']}"
            )

            sheet["D34"] = user_data["name"]
            sheet["D36"] = user_data["phone"]
            sheet["D38"] = full_address
            sheet["D41"] = selected_address['kode_pos']
            sheet["B45"] = user_data["courier"]
            sheet["B47"] = "IYA" if user_data["cod"] == "YES" else "TIDAK"

            workbook.save(output_path)
            workbook.close()

            with open(output_path, "rb") as file:
                bot.send_document(chat_id, file, caption=f"📦 Resi untuk {user_data['name']}")

            os.remove(output_path)
            bot.send_message(chat_id, "✅ Resi berhasil dikirim!")

        except Exception as e:
            bot.send_message(chat_id, f"❌ Gagal membuat resi: {str(e)}")
            if os.path.exists(output_path):
                os.remove(output_path)

    def create_courier_buttons(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("JNE", callback_data=f"COURIER_{user_id}_JNE"))
        markup.add(InlineKeyboardButton("J&T", callback_data=f"COURIER_{user_id}_J&T"))
        markup.add(InlineKeyboardButton("SiCepat", callback_data=f"COURIER_{user_id}_SiCepat"))
        markup.add(InlineKeyboardButton("Lion Parcel", callback_data=f"COURIER_{user_id}_LionParcel"))
        return markup

    def create_cod_buttons(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("COD ❌", callback_data=f"COD_{user_id}_NO"))
        markup.add(InlineKeyboardButton("COD ✅", callback_data=f"COD_{user_id}_YES"))
        return markup

    def sanitize_filename(filename):
        return re.sub(r'[\\/*?:"<>|]', "_", filename)

    bot.infinity_polling()
